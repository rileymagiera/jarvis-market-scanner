"""Forward stock paper experiment. Each symbol is an independent one-share observation."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
import os
from statistics import mean

from market_calendar import market_session_status, EASTERN
from signal_lead_time_audit import _positive, _time

MINUTE = timedelta(minutes=1)
QUOTE_WINDOW = timedelta(seconds=30)


class OpeningMarketData:
    """Read only Alpaca market data. A failed page invalidates the request."""

    def __init__(self, http, feed):
        self.http = http
        self.feed = feed
        self.headers = {
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
        }

    def read(self, kind, symbols, start, end):
        if self.feed not in {"iex", "sip"} or not all(self.headers.values()):
            raise ValueError("Live market data feed unavailable")
        result = defaultdict(list)
        names = {s.replace("-", "."): s for s in symbols}
        tickers = sorted(names)

        def fetch(group):
            params = dict(
                symbols=",".join(group),
                feed=self.feed,
                start=start.isoformat(),
                end=end.isoformat(),
                limit=10000,
                sort="asc",
            )
            if kind == "bars":
                params.update(timeframe="1Min", adjustment="raw")
            seen_tokens = set()
            while True:
                response = self.http.get(
                    "https://data.alpaca.markets/v2/stocks/" + kind,
                    headers=self.headers,
                    params=params,
                    timeout=(3, 8),
                )
                if response.status_code == 400 and "symbol" in response.text.lower():
                    # A malformed provider ticker must not discard the rest of the scan.
                    if len(group) > 1:
                        mid = len(group) // 2
                        fetch(group[:mid])
                        fetch(group[mid:])
                    return
                response.raise_for_status()
                page = response.json()
                for symbol, values in (page[kind] or {}).items():
                    if symbol in names:
                        result[names[symbol]].extend(values)
                token = page.get("next_page_token")
                if not token:
                    break
                if token in seen_tokens:
                    raise ValueError("Repeated market data page")
                seen_tokens.add(token)
                params["page_token"] = token

        for offset in range(0, len(tickers), 50):
            fetch(tickers[offset : offset + 50])
        return result


def rank_session(symbols, bars, start):
    moves = []
    for symbol in sorted(set(symbols)):
        valid = {}
        for bar in bars.get(symbol, []):
            stamp = _time(bar.get("t"))
            if (
                stamp
                and start <= stamp < start + 5 * MINUTE
                and _positive(bar.get("v"))
                and _positive(bar.get("o"))
                and _positive(bar.get("c"))
            ):
                valid[stamp] = bar
        if len(valid) >= 3:
            opening = float(valid[min(valid)]["o"])
            last = float(valid[max(valid)]["c"])
            moves.append(((last / opening - 1) * 100, symbol, last))
    moves.sort(key=lambda row: (-row[0], row[1]))
    rows = {symbol: {"selected": False} for symbol in sorted(set(symbols))}
    for rank, (move, symbol, close) in enumerate(moves, 1):
        rows[symbol].update(
            rank=rank, strength_pct=move, reference_close=close, selected=rank <= ceil(len(moves) / 10)
        )
    return rows


def first_quote(quotes, start):
    valid = []
    for q in quotes:
        t = _time(q.get("t"))
        bid, ask = _positive(q.get("bp")), _positive(q.get("ap"))
        if (
            t
            and start < t <= start + QUOTE_WINDOW
            and bid
            and ask
            and bid <= ask
            and _positive(q.get("bs"))
            and _positive(q.get("as"))
        ):
            valid.append((t, bid, ask))
    return min(valid, key=lambda row: row[0]) if valid else None


def exit_trigger(bars, row):
    """Inspect complete post-entry minutes; the next quote supplies the sell price."""
    entered = _time(row["entry_at"])
    start = entered.replace(second=0, microsecond=0) + MINUTE
    end = start + 60 * MINUTE
    valid = {}
    for bar in bars:
        stamp = _time(bar.get("t"))
        low, high = _positive(bar.get("l")), _positive(bar.get("h"))
        if stamp and start <= stamp < end and low and high and low <= high and _positive(bar.get("v")):
            valid[stamp] = (low, high)
    for stamp in sorted(valid):
        low, high = valid[stamp]
        if low <= row["ask"] * 0.97:
            return "stop", stamp + MINUTE
        if high >= row["ask"] * 1.08:
            return "target", stamp + MINUTE
    if valid and max(valid) >= end - 2 * MINUTE:
        return "horizon", end
    return "unknown_path", None


def settle(session, market, now):
    signal = _time(session["signal_at"])
    rows = session["rows"]
    if session["phase"] == "ranked" and now > signal + QUOTE_WINDOW:
        quotes = market.read("quotes", list(rows), signal, signal + QUOTE_WINDOW)
        for symbol, row in rows.items():
            quote = first_quote(quotes.get(symbol, []), signal)
            if not quote:
                row["outcome"] = "no_entry_quote"
                continue
            stamp, bid, ask = quote
            row.update(
                entry_at=stamp.isoformat(),
                bid=bid,
                ask=ask,
                spread_pct=100 * (ask - bid) / ((ask + bid) / 2),
                target=ask * 1.08,
                stop=ask * 0.97,
            )
            if "reference_close" in row:
                row["remaining_to_reference_target_pct"] = 100 * (
                    row["reference_close"] * 1.08 / ask - 1
                )
        session["phase"] = "entered"
    if session["phase"] not in ("entered", "exits_pending") or now < signal + 63 * MINUTE:
        return
    entries = {s: r for s, r in rows.items() if "entry_at" in r}
    if entries and session["phase"] == "entered":
        start = signal.replace(second=0, microsecond=0)
        bars = market.read("bars", list(entries), start, start + 63 * MINUTE)
        for symbol, row in entries.items():
            reason, at = exit_trigger(bars.get(symbol, []), row)
            row["outcome"] = reason
            if at:
                row["trigger_at"] = at.isoformat()
        session["phase"] = "exits_pending"
    exits = defaultdict(list)
    for symbol, row in entries.items():
        if row.get("trigger_at") and not row.get("exit_quote_checked"):
            exits[_time(row["trigger_at"])].append(symbol)
    if exits:
        from paper_trading import calculate_trade_fee

        for at, symbols in sorted(exits.items()):
            quotes = market.read("quotes", symbols, at, at + QUOTE_WINDOW)
            for symbol in symbols:
                row = entries[symbol]
                quote = first_quote(quotes.get(symbol, []), at)
                row["exit_quote_checked"] = True
                if quote:
                    stamp, bid, _ = quote
                    entry = row["ask"]
                    fees = sum(
                        calculate_trade_fee("stock", side, price, 1)["total"]
                        for side, price in [("buy", entry), ("sell", bid)]
                    )
                    row.update(
                        exit_at=stamp.isoformat(),
                        exit_bid=bid,
                        fees=fees,
                        net_return_pct=100 * (bid - entry - fees) / entry,
                    )
    session["phase"] = "complete"


def advance_opening_test(book, symbols, now, http, clock=lambda: datetime.now(timezone.utc)):
    """Freeze one ranking per session. Recover data failures on later scans."""
    sessions = book.setdefault("sessions", {})
    today = now.astimezone(EASTERN).date().isoformat()
    status = market_session_status(now)
    opening = _time(status.get("regular_open"))
    feed = os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower()
    if opening and status["is_regular_session"] and today not in sessions and symbols:
        start = opening + 3 * MINUTE
        deadline = opening + 30 * MINUTE
        if start + 5 * MINUTE <= now < deadline:
            data = OpeningMarketData(http, feed)
            bars = data.read("bars", symbols, start, start + 5 * MINUTE)
            rows = rank_session(symbols, bars, start)
            signal = clock()
            if signal < deadline and any(r["selected"] for r in rows.values()):
                book.pop("last_status", None)
                sessions[today] = dict(
                    signal_at=signal.isoformat(), feed=feed, phase="ranked", rows=rows
                )
            elif not any(r["selected"] for r in rows.values()):
                book["last_status"] = "No stocks had three traded minutes in the ranking window"
    if opening and now >= opening + 30 * MINUTE and today not in sessions:
        sessions[today] = dict(phase="missed_window", rows={})
    for day, session in sorted(sessions.items()):
        if session["phase"] not in ("ranked", "entered", "exits_pending"):
            continue
        try:
            settle(session, OpeningMarketData(http, session["feed"]), now)
            session.pop("data_error", None)
        except (ValueError, KeyError, OSError, TypeError) as error:
            session["data_error"] = type(error).__name__
            if now - _time(session["signal_at"]) > timedelta(days=3):
                session["phase"] = "data_unavailable"
    for old in sorted(sessions)[:-90]:
        del sessions[old]


def opening_summary(book):
    sessions = book.get("sessions", {})
    rows = [r for day in sessions.values() if day["phase"] == "complete" for r in day["rows"].values()]
    selected = [r for r in rows if r["selected"]]
    scored = [r for r in selected if r.get("outcome") in ("target", "stop", "horizon")]
    returns = [r["net_return_pct"] for r in selected if "net_return_pct" in r]
    return dict(
        sessions=len(sessions),
        completed=sum(d["phase"] == "complete" for d in sessions.values()),
        selected=len(selected),
        evaluated=len(scored),
        target_first=sum(r["outcome"] == "target" for r in scored),
        missed_runners=sum(not r["selected"] and r.get("outcome") == "target" for r in rows),
        controls_evaluated=sum(
            not r["selected"] and r.get("outcome") in ("target", "stop", "horizon") for r in rows
        ),
        net_count=len(returns),
        mean_net_pct=mean(returns) if returns else None,
    )
