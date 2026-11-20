"""Human-readable Jarvis notification formatting.

Email language distinguishes actual *simulated* paper-ledger trades from research
watchlist stages and official (unexecuted) BUY signals. No trade logic here.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_WATCH_ROWS = 8
MAX_PREMARKET_ROWS = 3


def _value(value, decimals=4):
    try:
        x = float(value)
        if not math.isfinite(x):
            return "unavailable"
        return f"{x:,.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{x:,.0f}"
    except (TypeError, ValueError, OverflowError):
        return "unavailable"


def _money(value, signed=False):
    try:
        x = float(value)
        if not math.isfinite(x):
            return "unavailable"
        return f"{x:+,.2f}" if signed else f"{x:,.2f}"
    except (TypeError, ValueError, OverflowError):
        return "unavailable"


def _price(value):
    val = _value(value, 8)
    return "$" + val if val != "unavailable" else "unavailable"


def _et(timestamp):
    if not timestamp:
        return "time unavailable"
    try:
        at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if at.tzinfo is None or at.utcoffset() is None:
            return "time unavailable"
        return at.astimezone(ZoneInfo("America/Detroit")).strftime("%b %-d, %-I:%M %p ET")
    except (ValueError, TypeError, OverflowError):
        return "time unavailable"


def event_fingerprint(event):
    """Stable key scoped to a paper ledger event; never modify the ledger."""
    fields = ("at", "type", "signal_id", "symbol", "quantity", "price", "realized_pnl")
    serialized = "\u001f".join(str(event.get(k, "")) for k in fields)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def current_paper_events(account, state, scan_at):
    """Keep all historically seen events from producing a new-email burst on rollout.

    Older unsent events *after* initialization are retained until a notification
    window. A fresh scan's events can be delivered on the first eligible window.
    """
    ledger = [e for e in (account.get("ledger") or [])
              if isinstance(e, dict) and e.get("type") in ("BUY", "SELL")]
    if "paper_email_seen_events" not in state:
        stamp = scan_at.isoformat() if hasattr(scan_at, "isoformat") else str(scan_at)
        state["paper_email_seen_events"] = [
            event_fingerprint(e) for e in ledger if e.get("at") != stamp
        ][-500:]
    seen = set(state.get("paper_email_seen_events") or [])
    return [event for event in ledger if event_fingerprint(event) not in seen]


def mark_paper_events_notified(state, events):
    seen = list(state.get("paper_email_seen_events") or [])
    for event in events:
        key = event_fingerprint(event)
        if key not in seen:
            seen.append(key)
    state["paper_email_seen_events"] = seen[-500:]


def format_paper_trades(events, account):
    """Return (subject, text); each event is a *paper* ledger BUY or SELL."""
    relevant = [e for e in events if e.get("type") in ("BUY", "SELL")]
    if not relevant:
        return None
    buys = sum(e["type"] == "BUY" for e in relevant)
    sells = sum(e["type"] == "SELL" for e in relevant)
    if len(relevant) == 1:
        event = relevant[0]
        action = "BOUGHT" if event["type"] == "BUY" else "SOLD"
        suffix = (" | P/L: $" + _money(event.get("realized_pnl"), signed=True)
                  if action == "SOLD" and event.get("realized_pnl") is not None else "")
        subject = f"Jarvis PAPER {action}: {event.get('symbol', '?')}{suffix}"
    else:
        subject = f"Jarvis PAPER TRADES: {buys} bought, {sells} sold"

    lines = [
        "JARVIS PAPER-TRADING ACTIVITY",
        "SIMULATED TRADES ONLY — no real money or broker orders.",
        "",
    ]
    for event in relevant:
        action = event["type"]
        symbol = str(event.get("symbol") or "unknown").upper()
        price = _price(event.get("price"))
        qty = _value(event.get("quantity"), 6)
        when = _et(event.get("at"))
        label = "PAPER BOUGHT" if action == "BUY" else "PAPER SOLD"
        lines.append(f"{label}: {symbol} @ {price}")
        lines.append(f"  When: {when} | Quantity: {qty} | Asset: {str(event.get('asset_type') or 'unknown').upper()}")
        lines.append(f"  Estimated fee/spread: ${_money(event.get('fee'))}")
        if action == "BUY":
            strategy = str(event.get("strategy") or "official_buy")
            lines.append("  Strategy: " + strategy.replace("_", " ") +
                         (" (EXPERIMENTAL)" if event.get("experimental") else " (official BUY paper simulation)"))
            lines.append("  Paper cost before fees: ${}".format(_money(event.get("notional"))))
            position = next((p for p in (account.get("positions") or []) + (account.get("closed_trades") or [])
                             if p.get("signal_id") == event.get("signal_id")), None)
            if position:
                lines.append(f"  Stop: {_price(position.get('stop_loss'))} | Target 1: {_price(position.get('target_1'))} | Target 2: {_price(position.get('target_2'))}")
        else:
            reason = str(event.get("message") or "Sell observed").split(" observed")[0]
            lines.append(f"  Reason: {reason}")
            if event.get("realized_pnl") is not None:
                lines.append("  Realized paper P/L after modeled costs: ${}".format(
                    _money(event.get("realized_pnl"), signed=True)))
            still_open = any(p.get("signal_id") == event.get("signal_id")
                             for p in account.get("positions") or [])
            lines.append("  Position: PARTIAL SALE, shares remain" if still_open else
                         "  Position: CLOSED (if this was the final quantity)")
        lines.append("")
    lines.extend([
        "All fills are paper simulations observed at scan prices; real execution,",
        "spreads and slippage could differ. No real account was touched.",
    ])
    return subject, "\n".join(lines)


def format_premarket_watch(items, timestamp):
    lines = [
        "JARVIS EARLY WATCH — NOT BOUGHT",
        f"Scan: {_et(timestamp)}",
        "These are research candidates only; no simulated or real purchase is implied.",
        "",
    ]
    for item in (items or [])[:MAX_PREMARKET_ROWS]:
        lines += [
            f"WATCH {item.get('symbol', '?')} | Premarket research score {_value(item.get('premarket_alert_score'), 0)}/100",
            f"  Price reference: {_price(item.get('price'))} | {_et(item.get('price_as_of'))}",
            f"  Quote status: {item.get('premarket_alert_status') or 'unavailable'}",
            "",
        ]
    if len(items or []) > MAX_PREMARKET_ROWS:
        lines.append(f"Plus {len(items) - MAX_PREMARKET_ROWS} more research candidates in Jarvis.")
    return "Jarvis WATCH ONLY: early candidates (no trades)", "\n".join(lines)


def format_daily_status(market, candidate_count, error_count, timestamp):
    regime = market.get("regime") or "unavailable"
    return (
        "Jarvis DAILY STATUS — NO NEW BUY SIGNALS\n"
        f"Scan: {_et(timestamp)}\n"
        f"Market regime: {regime}\n"
        f"Candidates scanned: {candidate_count}\n"
        f"Scanner issues: {error_count}\n\n"
        "WATCH ONLY: no new alert qualified. This does not imply a trade was placed.\n"
        "Paper transactions, if any, are sent in a separate clearly labeled email.\n"
        "No real orders are ever placed by Jarvis.\n"
    )


def format_market_digest(selected, premarket, market, candidate_count, error_count, timestamp):
    """Concise BUY-SIGNAL vs WATCH digest; never say 'bought' for research."""
    buys = [x for x in selected if x.get("stage") == "BUY"]
    others = [x for x in selected if x.get("stage") != "BUY"]
    ranked = sorted(buys, key=lambda x: -(float(x.get("score") or 0))) + sorted(
        others, key=lambda x: -(float(x.get("score") or 0)))
    if buys:
        subject = (f"Jarvis BUY SIGNAL (not executed): {buys[0].get('symbol')}"
                   if len(buys) == 1 else f"Jarvis: {len(buys)} BUY signals (not executed)")
    else:
        subject = f"Jarvis WATCH ONLY: {len(selected)} setups (no trades)"
    lines = [
        "JARVIS MARKET SIGNALS",
        f"Scan: {_et(timestamp)} | Regime: {market.get('regime') or 'unavailable'}",
        f"Research candidates: {len(selected)} | Official BUY signals: {len(buys)}",
        "BUY SIGNAL is a recommendation only, NOT a purchase. Actual paper trades",
        "are sent separately as PAPER BOUGHT / PAPER SOLD.",
        "",
    ]
    for item in ranked[:MAX_WATCH_ROWS]:
        official = item.get("stage") == "BUY"
        action = "BUY SIGNAL — NOT EXECUTED" if official else "WATCH ONLY — NOT BOUGHT"
        asset = str(item.get("asset_type") or "asset").upper()
        score = _value(item.get("score"), 0)
        lines += [
            f"{action}: {item.get('symbol') or '?'} ({asset})",
            f"  Stage: {item.get('stage') or 'WATCH'} | Score: {score}/100",
            f"  Price reference: {_price(item.get('price'))} at {_et(item.get('price_as_of'))}",
        ]
        quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
        lines.append("  Data: {} | {} | Source: {}".format(
            str(quality.get("data_type") or "unknown").upper(),
            item.get("price_validation_status") or "unknown",
            item.get("data_source") or "unknown"))
        levels = item.get("levels") if isinstance(item.get("levels"), dict) else {}
        if official and levels:
            lines.append(f"  Suggested entry: {_price(levels.get('watch_entry_low'))}–{_price(levels.get('watch_entry_high'))}")
            lines.append(f"  Stop: {_price(levels.get('stop_loss') or levels.get('invalidation_level'))} | Price Target 1: {_price(levels.get('target_1'))} | Price Target 2: {_price(levels.get('target_2'))}")
        else:
            lines.append("  Entry: WAIT for confirmed BUY + fresh eligible price; no paper purchase implied.")
        risks = item.get("risk_flags") or []
        if risks:
            lines.append("  Risk: " + str(risks[0])[:160])
        news = item.get("news") or []
        first = next((n for n in news if isinstance(n, dict) and n.get("title")), None)
        if first:
            lines.append("  Headline: " + str(first["title"])[:140])
        lines.append("")
    if len(ranked) > MAX_WATCH_ROWS:
        lines.append(f"Plus {len(ranked) - MAX_WATCH_ROWS} additional setups in the Jarvis dashboard.\n")
    if premarket:
        lines.append(f"Premarket research watches: {len(premarket)} (not BUY signals).")
        for item in premarket[:MAX_PREMARKET_ROWS]:
            lines.append("  {} | {} | {}".format(
                item.get("symbol") or "?", _price(item.get("price")),
                item.get("premarket_alert_status") or "status unknown"))
        lines.append("")
    lines.extend([
        f"Total scanned: {candidate_count} | Scan issues: {error_count}",
        "Paper-only Jarvis simulations are separate. No real orders are placed.",
    ])
    return subject, "\n".join(lines)
