"""Bounded research observations of open paper holdings, independent of scanner rank."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os

from signal_lead_time_audit import _positive, _time

LOOKBACK = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def _minute_bars(scanner, position, start, end):
    symbol = position["symbol"]
    if position["asset_type"] == "stock":
        key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_API_SECRET", "").strip()
        if not key or not secret:
            return "Alpaca IEX", "USD", "credentials_unavailable", []
        source, currency = "Alpaca IEX", "USD"
        response = scanner.HTTP.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            params={"feed": "iex", "timeframe": "1Min", "start": start.isoformat(),
                    "end": end.isoformat(), "limit": 100},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=(2, 4),
        )
        response.raise_for_status()
        rows = response.json().get("bars") or []
        bars = [(_time(row.get("t")), row.get("h"), row.get("l"), row.get("c")) for row in rows]
    else:
        base = scanner.crypto_base(symbol)
        entry_source = str(position.get("price_source") or "").lower()
        if "coinbase" in entry_source:
            source, currency = "Coinbase Exchange", "USD"
            response = scanner.HTTP.get(
                f"https://api.exchange.coinbase.com/products/{base}-USD/candles",
                params={"granularity": 60, "start": start.isoformat(), "end": end.isoformat()},
                timeout=(2, 4),
            )
            response.raise_for_status()
            bars = [(datetime.fromtimestamp(row[0], timezone.utc), row[2], row[1], row[4])
                    for row in response.json() if isinstance(row, list) and len(row) >= 6]
        else:
            source = "Kraken Spot"
            pair = scanner.kraken_pairs().get(base)
            currency = scanner.KRAKEN_PAIR_QUOTE_MAP.get(base)
            if not pair or currency not in {"USD", "USDT"}:
                return source, currency, "pair_unavailable", []
            response = scanner.kraken_get(
                f"{scanner.KRAKEN}/OHLC",
                params={"pair": pair, "interval": 1, "since": int(start.timestamp())},
                timeout=(2, 4),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                return source, currency, "provider_error", []
            rows = next((rows for key, rows in (payload.get("result") or {}).items()
                         if key != "last"), [])
            bars = [(datetime.fromtimestamp(row[0], timezone.utc), row[2], row[3], row[4])
                    for row in rows if isinstance(row, list) and len(row) >= 8]
    return source, currency, "ok", bars


def _scan_quote(candidate, now):
    quote = candidate.get("stock_execution_quote")
    if not isinstance(quote, dict):
        quote = {}
    stamp = _time(quote.get("as_of"))
    bid, ask = _positive(quote.get("bid")), _positive(quote.get("ask"))
    if stamp and 0 <= (now - stamp).total_seconds() <= 300 and bid and ask and bid <= ask:
        return {"kind": "bid_ask_snapshot", "as_of": stamp.isoformat(),
                "bid": bid, "ask": ask, "source": quote.get("feed")}
    stamp = _time(candidate.get("price_as_of"))
    price = _positive(candidate.get("price"))
    if (candidate.get("price_validation_passed") is True and stamp and price
            and 0 <= (now - stamp).total_seconds() <= 300):
        return {"kind": "verified_scan_price", "as_of": stamp.isoformat(),
                "price": price, "source": candidate.get("price_source")}
    return None


def _observe(scanner, position, candidate, start, now):
    # Holders can fall out of the stock universe. A quote from the same
    # configured IEX feed still helps audit their sell-side price.
    scan_quote = _scan_quote(candidate, now)
    if (scan_quote is None and position["asset_type"] == "stock"
            and os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower() == "iex"):
        try:
            quote = scanner.alpaca_latest_quote(position["symbol"])
            scan_quote = _scan_quote({"stock_execution_quote": quote}, datetime.now(timezone.utc))
        except Exception:
            # Minute bar collection is independent of spot quote availability.
            pass
    end = now.replace(second=0, microsecond=0)
    result = {"asset_type": position["asset_type"], "symbol": position["symbol"],
              "opened_at": position.get("opened_at"),
              "in_market_scan": bool(candidate), "quote": scan_quote,
              "bars": [], "status": "no_completed_minute"}
    opened = _time(position.get("opened_at"))
    if opened is None:
        result["status"] = "missing_open_time"
        return result
    first_minute = opened.replace(second=0, microsecond=0)
    if first_minute < opened:
        first_minute += MINUTE
    start = max(start, first_minute)
    result.update(window_start=start.isoformat(), window_end=end.isoformat())
    if start >= end:
        return result
    try:
        source, currency, status, raw = _minute_bars(scanner, position, start, end)
    except Exception as exc:
        result["status"] = "provider_unavailable"
        result["error_type"] = type(exc).__name__
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code:
            result["http_status"] = status_code
        return result
    result.update(source=source, quote_currency=currency, status=status)
    bars = {}
    stop, target_1, target_2 = (_positive(position.get(level)) for level in ("stop_loss", "target_1", "target_2"))
    for stamp, high, low, close in raw:
        high, low, close = map(_positive, (high, low, close))
        if not (stamp and start <= stamp < end and high and low and close and low <= close <= high):
            continue
        touched_stop = stop is not None and low <= stop
        touched_first = target_1 is not None and high >= target_1
        touched_second = target_2 is not None and high >= target_2
        bars[stamp] = {"at": stamp.isoformat(), "high": high, "low": low, "close": close,
                       "stop_touched": touched_stop, "target_1_touched": touched_first,
                       "target_2_touched": touched_second,
                       "stop_and_target_same_bar": touched_stop and (touched_first or touched_second)}
    result["bars"] = [bars[stamp] for stamp in sorted(bars)]
    result["completed_minutes_requested"] = (end - start) // MINUTE
    result["minutes_with_bars"] = len(bars)
    if status == "ok":
        result["status"] = "observed" if bars else "no_bars_returned"
    return result


def collect_paper_position_watch(scanner, positions, candidates, now, previous_scan_at):
    """Capture completed minutes from the prior scan boundary, with no simulated fills."""
    end = now.replace(second=0, microsecond=0)
    prior = _time(previous_scan_at)
    start = max(end - LOOKBACK, prior.replace(second=0, microsecond=0)) if prior else end - LOOKBACK
    by_key = {(row.get("asset_type"), str(row.get("symbol") or "").upper()): row for row in candidates}
    held = [{**row, "symbol": str(row["symbol"]).upper()} for row in positions
            if isinstance(row, dict) and row.get("asset_type") in {"stock", "crypto"} and row.get("symbol")]
    jobs = [(position, by_key.get((position["asset_type"], position["symbol"]), {})) for position in held]
    rows = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            rows = list(pool.map(lambda job: _observe(scanner, *job, start, now), jobs))
    return {
        "mode": "read_only_paper_positions",
        "captured_at": now.isoformat(),
        "window_clipped": prior is None or prior < end - LOOKBACK,
        "positions_watched": len(rows),
        "positions_outside_candidate_list": sum(not row["in_market_scan"] for row in rows),
        "positions_with_minute_bars": sum(bool(row["bars"]) for row in rows),
        "positions": rows,
        "interpretation": "Completed trade bars show observed level crossings. Quote and intraminute fill order are unknown; no paper or real orders are triggered.",
    }
