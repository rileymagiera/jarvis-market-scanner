"""Consolidate routine emails. No market, strategy, or paper-trading changes."""
from notification_format import format_daily_status, format_market_digest, format_premarket_watch

MAX_RESEARCH_DIGEST_ROWS = 80


def _compact(item):
    keys = ("asset_type", "symbol", "stage", "signal", "score", "price",
            "price_as_of", "price_validation_status", "data_source",
            "premarket_alert_score", "premarket_alert_status")
    row = {key: item.get(key) for key in keys if key in item}
    quality = item.get("data_quality")
    if isinstance(quality, dict):
        row["data_quality"] = {"data_type": quality.get("data_type")}
    levels = item.get("levels")
    if isinstance(levels, dict):
        names = ("watch_entry_low", "watch_entry_high", "stop_loss",
                 "invalidation_level", "target_1", "target_2")
        row["levels"] = {key: levels.get(key) for key in names if key in levels}
    news = item.get("news") or []
    if isinstance(news, list):
        row["news"] = [{"title": str(n.get("title", ""))[:150]}
                       for n in news[:2] if isinstance(n, dict) and n.get("title")]
    risks = item.get("risk_flags") or []
    if isinstance(risks, list):
        row["risk_flags"] = [str(x)[:160] for x in risks[:1]]
    return row


def _bound(items):
    if len(items) > MAX_RESEARCH_DIGEST_ROWS:
        weakest = sorted(items, key=lambda key: float(
            items[key].get("score") or items[key].get("premarket_alert_score") or 0))
        for key in weakest[:-MAX_RESEARCH_DIGEST_ROWS]:
            del items[key]


def queue_research_digest(state, watch, premarket):
    """Save unique watch items across scans; leave all market data unchanged."""
    pending = state.setdefault("email_digest_pending", {})
    watches = pending.setdefault("watch", {})
    early = pending.setdefault("premarket", {})
    for item in watch:
        if item.get("symbol"):
            watches[f"{item.get('asset_type', 'asset')}:{item['symbol']}"] = _compact(item)
    for item in premarket:
        if item.get("symbol"):
            early[str(item["symbol"])] = _compact(item)
    _bound(watches)
    _bound(early)


def queue_paper_digest_events(state, events):
    counts = state.setdefault("email_digest_pending", {}).setdefault(
        "paper_events", {"BUY": 0, "SELL": 0})
    for event in events:
        action = str(event.get("type") or "").upper()
        if action in ("BUY", "SELL"):
            counts[action] = counts.get(action, 0) + 1


def queue_buy_digest_signals(state, buys):
    rows = state.setdefault("email_digest_pending", {}).setdefault("buy_signals", {})
    for item in buys:
        if item.get("symbol"):
            rows[f"{item.get('asset_type', 'asset')}:{item['symbol']}"] = _compact(item)
    _bound(rows)


def digest_due(local_now, state):
    """First successful scan during the 8 PM Eastern hour; once per date."""
    return (local_now.hour == 20 and
            state.get("email_digest_sent_date") != local_now.date().isoformat())


def format_daily_digest(state, local_now, market, candidate_count, error_count,
                        timestamp, paper_summary):
    pending = state.get("email_digest_pending") or {}
    watch = list((pending.get("watch") or {}).values())
    premarket = list((pending.get("premarket") or {}).values())
    buys = list((pending.get("buy_signals") or {}).values())
    paper = pending.get("paper_events") or {}
    if watch:
        _, research_body = format_market_digest(
            watch, premarket, market, candidate_count, error_count, timestamp)
    elif premarket:
        _, research_body = format_premarket_watch(premarket, timestamp)
    else:
        research_body = format_daily_status(
            market, candidate_count, error_count, timestamp)
    subject = f"Jarvis DAILY DIGEST: {len(watch)} watches, {len(buys)} BUY signals"
    dollar = "$"
    lines = [
        "JARVIS EVENING DIGEST — " + local_now.strftime("%b %d, %Y") + " Eastern",
        "Routine research updates consolidated. Prompt BUY recommendations and",
        "simulated paper trades remain separate, subject to existing quiet hours.",
        "",
        f"New research setups since previous digest: {len(watch)}",
        f"Premarket watches: {len(premarket)}",
        f"BUY recommendations already emailed (NOT executed): {len(buys)}",
        f"Paper trades already emailed: {paper.get('BUY', 0)} bought / {paper.get('SELL', 0)} sold",
        "",
        "PAPER ACCOUNT (simulation only)",
        f"Equity: {dollar}{paper_summary.get('equity', 0):,.2f}",
        f"Return: {paper_summary.get('total_return_pct', 0):+.2f}%",
        f"Open positions: {paper_summary.get('open_positions', 0)}",
        f"Realized P/L: {dollar}{paper_summary.get('realized_pnl', 0):+,.2f}",
        f"Unrealized P/L: {dollar}{paper_summary.get('unrealized_pnl', 0):+,.2f}",
        "",
        "SYSTEM HEALTH",
        f"Last scan issues: {error_count} | Last scan candidates: {candidate_count}",
        "Check the Jarvis dashboard/GitHub Actions for detailed failure diagnostics.",
        "",
        research_body,
    ]
    return subject, "\n".join(lines)


def mark_digest_sent(state, local_now):
    state["email_digest_sent_date"] = local_now.date().isoformat()
    state["email_digest_pending"] = {}
