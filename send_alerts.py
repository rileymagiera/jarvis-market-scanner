import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path("alert_state.json")
PAPER_STATE = Path("paper_tracker.json")
PAPER_LIFETIME_STATE = Path("paper_tracker_lifetime.json")
BASE_URL = os.getenv("JARVIS_SCANNER_URL", "https://jarvis-market-scanner.onrender.com").rstrip("/")
SENDER = os.environ["JARVIS_GMAIL_USER"]
PASSWORD = os.environ["JARVIS_GMAIL_APP_PASSWORD"].replace(" ", "")
RECIPIENT = os.environ["JARVIS_ALERT_TO"]


def email(subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, RECIPIENT, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(SENDER, PASSWORD)
        smtp.send_message(msg)


def update_paper_tracker(candidates, now):
    """Track recommendations with 365-day detail and cumulative lifetime checkpoint stats."""
    try:
        tracker = json.loads(PAPER_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        tracker = {}
    try:
        lifetime = json.loads(PAPER_LIFETIME_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        lifetime = {"recommendations": 0, "by_asset": {}, "checkpoints": {}}

    summaries = {}
    for item in candidates:
        asset_type = item.get("asset_type", "asset")
        symbol = item.get("symbol", "unknown")
        key = f"{asset_type}:{symbol}"
        price = float(item.get("price") or 0)
        if price <= 0:
            continue
        record = tracker.get(key)
        if not record:
            record = {
                "asset_type": asset_type, "symbol": symbol,
                "first_seen": now.isoformat(), "initial_price": price,
                "peak_price": price, "trough_price": price, "milestones": {},
            }
            lifetime["recommendations"] = int(lifetime.get("recommendations", 0)) + 1
            asset_stats = lifetime.setdefault("by_asset", {}).setdefault(asset_type, {"recommendations": 0})
            asset_stats["recommendations"] += 1

        initial = float(record.get("initial_price") or price)
        record["latest_price"] = price
        record["peak_price"] = max(float(record.get("peak_price") or price), price)
        record["trough_price"] = min(float(record.get("trough_price") or price), price)
        first_seen = datetime.fromisoformat(record["first_seen"])
        age_days = max(0, (now - first_seen).total_seconds() / 86400)
        record["age_days"] = round(age_days, 2)
        record["return_pct"] = round((price / initial - 1) * 100, 2)
        record["max_gain_pct"] = round((float(record["peak_price"]) / initial - 1) * 100, 2)
        record["max_drawdown_pct"] = round((float(record["trough_price"]) / initial - 1) * 100, 2)
        for horizon in (1, 3, 5, 20):
            milestone = str(horizon)
            if age_days >= horizon and milestone not in record["milestones"]:
                checkpoint_return = record["return_pct"]
                record["milestones"][milestone] = checkpoint_return
                stats = lifetime.setdefault("checkpoints", {}).setdefault(
                    milestone, {"observations": 0, "wins": 0, "losses": 0, "flat": 0, "total_return_pct": 0.0}
                )
                stats["observations"] += 1
                stats["wins"] += int(checkpoint_return > 0)
                stats["losses"] += int(checkpoint_return < 0)
                stats["flat"] += int(checkpoint_return == 0)
                stats["total_return_pct"] = round(float(stats["total_return_pct"]) + checkpoint_return, 2)
                asset_checkpoints = lifetime.setdefault("by_asset", {}).setdefault(
                    asset_type, {"recommendations": 0}
                ).setdefault("checkpoints", {})
                asset_stats = asset_checkpoints.setdefault(
                    milestone, {"observations": 0, "wins": 0, "losses": 0, "flat": 0, "total_return_pct": 0.0}
                )
                asset_stats["observations"] += 1
                asset_stats["wins"] += int(checkpoint_return > 0)
                asset_stats["losses"] += int(checkpoint_return < 0)
                asset_stats["flat"] += int(checkpoint_return == 0)
                asset_stats["total_return_pct"] = round(float(asset_stats["total_return_pct"]) + checkpoint_return, 2)
        record["last_seen"] = now.isoformat()
        record["last_stage"] = item.get("stage", item.get("signal", "WATCH"))
        tracker[key] = record
        summaries[key] = record

    # Keep one year of detailed recommendations; lifetime aggregates remain cumulative.
    cutoff = now - timedelta(days=365)
    tracker = {
        key: value for key, value in tracker.items()
        if datetime.fromisoformat(value.get("last_seen", value.get("first_seen"))) >= cutoff
    }
    PAPER_STATE.write_text(json.dumps(tracker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PAPER_LIFETIME_STATE.write_text(json.dumps(lifetime, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summaries, lifetime


def main():
    if "--test" in sys.argv:
        email("Jarvis email test", "Jarvis successfully sent a test email. Email delivery is connected. This test does not confirm market scanning. Jarvis is recommendation-only and never places trades.")
        print("Test email sent.")
        return

    req = Request(BASE_URL + "/scan?asset=all", headers={"User-Agent": "JarvisAlertWorkflow/1.0"})
    with urlopen(req, timeout=240) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload.get("candidates"), list):
        raise RuntimeError("Scanner response did not contain candidates.")

    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    now = datetime.now(timezone.utc)
    paper, paper_lifetime = update_paper_tracker(payload["candidates"], now)
    market = payload.get("market_conditions") or {}
    selected, updates = [], {}
    for item in payload["candidates"]:
        score = int(item.get("score") or 0)
        volume = float(item.get("relative_volume") or 0)
        intraday_volume = float(item.get("intraday_relative_volume") or 0)
        breakout = bool(item.get("breakout_20d") or item.get("breakout_24h"))
        intraday_breakout = bool(item.get("intraday_breakout"))
        if score < 60 or not (volume >= 1.5 or breakout or intraday_volume >= 2 or intraday_breakout):
            continue
        key = f'{item.get("asset_type", "asset")}:{item.get("symbol", "unknown")}'
        previous = state.get(key, {})
        previous_score = int(previous.get("score") or 0)
        last = previous.get("alerted_at")
        expired = True
        if last:
            try:
                expired = now - datetime.fromisoformat(last) >= timedelta(hours=24)
            except ValueError:
                pass
        if not previous or expired or (score >= 75 and previous_score < 75) or score >= previous_score + 12:
            selected.append(item)
            updates[key] = {"score": score, "alerted_at": now.isoformat()}

    if not selected:
        # Send one heartbeat per UTC day when there are no new qualifying setups.
        daily_key = now.date().isoformat()
        if state.get("daily_status_date") == daily_key:
            print("No new qualifying setups; daily status email already sent today.")
            return
        generated = str(payload.get("timestamp", now.isoformat()))
        candidates = payload.get("candidates_found", len(payload["candidates"]))
        errors = payload.get("errors") or []
        body = "\n".join([
            "JARVIS DAILY MARKET STATUS",
            "Generated: " + generated,
            "",
            "Jarvis completed its scheduled stock and crypto scan.",
            "No new setups met the current alert criteria today.",
            "Market regime: " + str(market.get("regime", "unavailable")),
            f"Candidates returned: {candidates}",
            f"Paper-tracked candidates: {len(paper)}",
            f"Scanner errors reported: {len(errors)}",
            "",
            "This is a monitoring update, not a buy or sell instruction.",
            "Jarvis is recommendation-only and never places trades.",
        ])
        email("Jarvis daily status: no new setups", body)
        state["daily_status_date"] = daily_key
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("Daily no-new-setups status email sent.")
        return

    lines = [
        "JARVIS MARKET SCANNER — NEW SETUPS",
        "Generated: " + str(payload.get("timestamp", now.isoformat())),
        "",
        "Research watchlist only. Not personalized financial advice. No trades are placed.",
        "Verify current price, liquidity, news and risk before making any decision.",
        "",
    ]
    for item in sorted(selected, key=lambda x: int(x.get("score") or 0), reverse=True):
        lines += [
            f'{item.get("asset_type", "asset").upper()} {item.get("symbol", "")} | Score {item.get("score", "n/a")}/100 | {item.get("stage", item.get("signal", "WATCH"))}',
            f'Market regime: {item.get("market_regime", market.get("regime", "unavailable"))} | Raw score: {item.get("raw_score", item.get("score", "n/a"))} | Regime adjustment: {item.get("market_score_adjustment", 0)}',
            f'Price: {item.get("price", "n/a")} | Daily/24h change: {item.get("daily_change_pct", "n/a")}% | Relative volume: {item.get("relative_volume", "n/a")}x',
            f'Intraday 15m change: {item.get("intraday_change_pct", "n/a")}% | Intraday relative volume: {item.get("intraday_relative_volume", "n/a")}x | Intraday breakout: {item.get("intraday_breakout", False)}',
            f'Breakout: {item.get("breakout_20d", item.get("breakout_24h", False))} | Source: {item.get("data_source", "n/a")}',
        ]
        levels = item.get("levels") or {}
        if levels:
            lines.append(f'Watch range: {levels.get("watch_entry_low", "n/a")}–{levels.get("watch_entry_high", "n/a")} | Illustrative invalidation: {levels.get("invalidation_level", "n/a")}')
        tracked = paper.get(f'{item.get("asset_type", "asset")}:{item.get("symbol", "unknown")}')
        if tracked:
            lines.append(
                f'PAPER TRACKER: first seen {tracked.get("first_seen")} | return {tracked.get("return_pct")}% | '
                f'best move {tracked.get("max_gain_pct")}% | worst move {tracked.get("max_drawdown_pct")}% | '
                f'age {tracked.get("age_days")} days | 1/3/5/20d checkpoints {tracked.get("milestones", {})}'
            )
        for news in (item.get("news") or [])[:4]:
            if news.get("title"):
                source = (" [" + news["publisher"] + "]") if news.get("publisher") else ""
                lines.append("Catalyst: " + news["title"] + source + ((" — " + news["url"]) if news.get("url") else ""))
        for flag in (item.get("risk_flags") or []):
            lines.append("RISK REVIEW: " + flag)
        lines.append("")
    lines += [f'Scan candidates: {payload.get("candidates_found", "n/a")}', f'Scan errors: {len(payload.get("errors") or [])}']
    email(f"Jarvis: {len(selected)} new market setup(s)", "\n".join(lines))
    state.update(updates)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Sent {len(selected)} alert(s).")


if __name__ == "__main__":
    main()
