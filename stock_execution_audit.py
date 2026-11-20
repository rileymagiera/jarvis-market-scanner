"""Read-only diagnostics for saved Alpaca stock quotes and research trade plans."""
from collections import Counter
from math import isfinite

from early_opportunity_radar import _quote_age_seconds
from entry_timing_replay import frozen_plan
from paper_trading import calculate_trade_fee

MAX_QUOTE_AGE_SECONDS = 60
MAX_STOP_RISK = 0.04
MIN_NET_REWARD_RISK = 1.5
EXIT_FRICTION_BPS = 25
MAX_EXAMPLES = 12


def _maximum_ask(plan):
    _, high, stop, target, _ = plan
    # The existing stock model charges proportional fees. The ask already
    # includes the opening spread, so apply the friction assumption on exit.
    buy_fee_factor = 1 + calculate_trade_fee("stock", "buy", 100)["total"] / 100
    sell_fee_factor = 1 - calculate_trade_fee("stock", "sell", 100)["total"] / 100
    exit_factor = sell_fee_factor * (1 - EXIT_FRICTION_BPS / 10_000)
    net_stop, net_target = stop * exit_factor, target * exit_factor
    bounds = {
        "saved_entry_range": high,
        "stop_risk": net_stop / ((1 - MAX_STOP_RISK) * buy_fee_factor),
        "net_reward_to_risk": (
            (net_target + MIN_NET_REWARD_RISK * net_stop)
            / ((1 + MIN_NET_REWARD_RISK) * buy_fee_factor)
        ),
    }
    binding = min(bounds, key=bounds.get)
    return bounds[binding], binding


def _audit_quote(snapshot):
    quote = snapshot.get("stock_execution_quote")
    if not isinstance(quote, dict):
        return {"status": "no_saved_quote"}
    feed = quote.get("feed")
    as_of = quote.get("as_of")
    bid, ask = quote.get("bid"), quote.get("ask")
    age = _quote_age_seconds(snapshot.get("observed_at"), as_of)
    result = {"feed": feed, "quote_as_of": as_of, "quote_age_seconds": age,
              "bid": bid, "ask": ask, "bid_size": quote.get("bid_size"),
              "ask_size": quote.get("ask_size")}
    if feed not in {"iex", "sip"}:
        return {**result, "status": "unsupported_feed"}
    if not (type(bid) in (int, float) and type(ask) in (int, float)
            and isfinite(bid) and isfinite(ask) and 0 < bid <= ask):
        return {**result, "status": "unusable_bid_ask"}
    if age is None or age > MAX_QUOTE_AGE_SECONDS:
        return {**result, "status": "quote_too_old_or_untimed"}
    result["spread_bps"] = round(10_000 * (ask - bid) / ((bid + ask) / 2), 2)
    _, plan = frozen_plan(snapshot)
    if plan is None:
        return {**result, "status": "no_complete_saved_plan"}

    low, _, stop, _, _ = plan
    ceiling, binding = _maximum_ask(plan)
    result.update(maximum_ask=round(ceiling, 6), binding_upper_limit=binding,
                  ask_minus_maximum=round(ask - ceiling, 6))
    if ceiling < low or ceiling <= stop:
        status = "no_price_meets_plan"
    elif ask < low or ask <= stop:
        status = "ask_below_entry_range"
    elif ask > ceiling:
        status = "ask_above_maximum"
    else:
        status = "ask_within_modeled_range"
    return {**result, "status": status}


def build_stock_execution_audit(records, scan_id, symbols=None):
    """Summarize the latest specified scan; keep individual examples bounded."""
    counts = Counter()
    examples = []
    for record in records:
        if record.get("asset_type") != "stock" or not scan_id:
            continue
        snapshot = next((s for s in reversed(record.get("signal_history") or [])
                         if isinstance(s, dict) and str(s.get("scan_id")) == str(scan_id)), None)
        if snapshot is None:
            continue
        result = _audit_quote(snapshot)
        counts[result["status"]] += 1
        include = (record.get("symbol") in symbols) if symbols is not None else ("spread_bps" in result)
        if include and len(examples) < MAX_EXAMPLES:
            examples.append({"symbol": record.get("symbol"),
                             "research_phase": snapshot.get("shadow_classification"), **result})
    return {
        "version": "alpaca-stock-execution-quotes-v1",
        "scan_run_id": str(scan_id) if scan_id else None,
        "stock_research_snapshots": sum(counts.values()),
        "counts": dict(counts),
        "examples": examples,
        "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
        "minimum_net_reward_risk": MIN_NET_REWARD_RISK,
        "max_net_stop_risk_pct": 100 * MAX_STOP_RISK,
        "modeled_exit_friction_bps": EXIT_FRICTION_BPS,
        "limitations": [
            "Alpaca IEX quotes are venue-limited. SIP coverage depends on entitlement.",
            "The ceiling uses the first saved target, saved stop, quoted ask, regulatory fees and modeled exit friction.",
            "A saved ask describes one observation. Quote availability, queue priority and fills remain unknown.",
        ],
    }
