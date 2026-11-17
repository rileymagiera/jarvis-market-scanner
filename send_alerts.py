import base64
import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path("alert_state.json")
PAPER_STATE = Path("paper_tracker.json")
PAPER_LIFETIME_STATE = Path("paper_tracker_lifetime.json")
BUY_SIGNAL_STATE = Path("buy_signal_history.json")
DIAGNOSTICS_STATE = Path("scan_diagnostics.json")
BASE_URL = os.getenv("JARVIS_SCANNER_URL", "https://jarvis-market-scanner.onrender.com").rstrip("/")
SENDER = os.environ["JARVIS_GMAIL_USER"]
PASSWORD = os.environ["JARVIS_GMAIL_APP_PASSWORD"].replace(" ", "")
RECIPIENT = os.environ["JARVIS_ALERT_TO"]
SCANNER_USERNAME = os.getenv("JARVIS_SITE_USERNAME", "")
SCANNER_PASSWORD = os.getenv("JARVIS_SITE_PASSWORD", "")


def email(subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, RECIPIENT, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(SENDER, PASSWORD)
        smtp.send_message(msg)


def sync_history_to_site(paper, lifetime, now):
    token = os.getenv("JARVIS_HISTORY_SYNC_TOKEN", "")
    if not token:
        print("History sync skipped: JARVIS_HISTORY_SYNC_TOKEN is not configured.")
        return
    body = json.dumps({
        "synced_at": now.isoformat(),
        "paper_tracker": paper,
        "lifetime": lifetime,
    }).encode("utf-8")
    req = Request(
        BASE_URL + "/history/sync",
        data=body,
        headers={"Content-Type": "application/json", "X-Jarvis-History-Token": token},
        method="POST",
    )
    with urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    print(f"History synced to website: {result.get('records', 0)} records.")


def _history_context(item):
    reasons = []
    rv = float(item.get("relative_volume") or 0)
    irv = float(item.get("intraday_relative_volume") or 0)
    if rv >= 1.5: reasons.append(f"Relative volume {rv:.1f}x")
    if irv >= 2: reasons.append(f"Intraday relative volume {irv:.1f}x")
    if item.get("breakout_20d") or item.get("breakout_24h"): reasons.append("Prior-range breakout")
    if item.get("intraday_breakout"): reasons.append("Intraday breakout")
    if float(item.get("daily_change_pct") or 0) >= 2: reasons.append("Positive daily momentum")
    if float(item.get("five_day_change_pct") or 0) >= 5: reasons.append("Strong 5-day momentum")
    if not reasons: reasons.append("Momentum / technical scan threshold")
    catalysts = []
    for news in (item.get("news") or [])[:3]:
        if news.get("title"):
            catalysts.append({k: news.get(k) for k in ("title", "publisher", "url", "published_at", "source_type", "form")})
    return reasons, catalysts


def update_paper_tracker(candidates, now):
    """Track recommendations with 365-day detail and cumulative lifetime checkpoint stats."""
    try:
        tracker = json.loads(PAPER_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        tracker = {}
    try:
        lifetime = json.loads(PAPER_LIFETIME_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Seed lifetime totals from existing detailed records when introducing this file.
        lifetime = {"recommendations": len(tracker), "by_asset": {}, "checkpoints": {}}
        for old_record in tracker.values():
            old_asset = old_record.get("asset_type", "asset")
            old_asset_stats = lifetime["by_asset"].setdefault(old_asset, {"recommendations": 0, "checkpoints": {}})
            old_asset_stats["recommendations"] += 1
            for horizon, raw_return in (old_record.get("milestones") or {}).items():
                try:
                    old_return = float(raw_return)
                except (TypeError, ValueError):
                    continue
                old_global = lifetime["checkpoints"].setdefault(
                    str(horizon), {"observations": 0, "wins": 0, "losses": 0, "flat": 0, "total_return_pct": 0.0}
                )
                old_global["observations"] += 1
                old_global["wins"] += int(old_return > 0)
                old_global["losses"] += int(old_return < 0)
                old_global["flat"] += int(old_return == 0)
                old_global["total_return_pct"] = round(old_global["total_return_pct"] + old_return, 2)
                old_asset_checkpoint = old_asset_stats["checkpoints"].setdefault(
                    str(horizon), {"observations": 0, "wins": 0, "losses": 0, "flat": 0, "total_return_pct": 0.0}
                )
                old_asset_checkpoint["observations"] += 1
                old_asset_checkpoint["wins"] += int(old_return > 0)
                old_asset_checkpoint["losses"] += int(old_return < 0)
                old_asset_checkpoint["flat"] += int(old_return == 0)
                old_asset_checkpoint["total_return_pct"] = round(old_asset_checkpoint["total_return_pct"] + old_return, 2)

    # Mark prior records absent until they are rediscovered in this scan.
    for existing in tracker.values():
        existing["currently_surfaced"] = False
        if existing.get("asset_type") == "stock" and existing.get("price_validation_status") is None:
            existing["price_validation_status"] = "unverified"
            existing["price_validation_note"] = "Legacy stock price was not independently validated"
            existing["unverified_return_pct"] = existing.get("return_pct")
            existing["return_pct"] = None
            existing["max_gain_pct"] = None
            existing["max_drawdown_pct"] = None
            existing["latest_price"] = None

    summaries = {}
    for item in candidates:
        asset_type = item.get("asset_type", "asset")
        symbol = item.get("symbol", "unknown")
        key = f"{asset_type}:{symbol}"
        price = float(item.get("price") or 0)
        if price <= 0:
            continue
        record = tracker.get(key)
        if asset_type == "stock" and not item.get("price_validation_passed", False):
            if record:
                record["price_validation_status"] = "unverified"
                record["price_validation_note"] = item.get("price_validation_note", "Independent quote validation unavailable")
                record["unverified_return_pct"] = record.get("return_pct")
                record["return_pct"] = None
                record["max_gain_pct"] = None
                record["max_drawdown_pct"] = None
                record["latest_price"] = None
                record["currently_surfaced"] = True
                tracker[key] = record
            continue
        if record and record.get("price_validation_status") == "unverified":
            record["quarantined_initial_price"] = record.get("initial_price")
            record["quarantined_return_pct"] = record.get("unverified_return_pct")
            record["first_seen"] = now.isoformat()
            record["initial_price"] = price
            record["peak_price"] = price
            record["trough_price"] = price
            record["price_history"] = []
            record["milestones"] = {}
            record["return_pct"] = 0.0
            record["max_gain_pct"] = 0.0
            record["max_drawdown_pct"] = 0.0
            record["price_validation_status"] = "verified"
            record["price_validation_note"] = "Baseline reset after independent price verification"
        if not record:
            record = {
                "asset_type": asset_type, "symbol": symbol,
                "first_seen": now.isoformat(), "initial_price": price,
                "peak_price": price, "trough_price": price, "milestones": {},
                "first_grade": item.get("stage", item.get("signal", "WATCH")),
                "grade_history": [{"from": None, "to": item.get("stage", item.get("signal", "WATCH")), "at": now.isoformat()}],
                "price_history": [], "discovery_reasons": [], "catalysts": [], "risk_flags": [],
            }
            lifetime["recommendations"] = int(lifetime.get("recommendations", 0)) + 1
            asset_stats = lifetime.setdefault("by_asset", {}).setdefault(asset_type, {"recommendations": 0})
            asset_stats["recommendations"] += 1

        initial = float(record.get("initial_price") or price)
        record["latest_price"] = price
        record["price_validation_status"] = "verified" if asset_type == "stock" else "not_required"
        record["price_validation_note"] = item.get("price_validation_note", "Not applicable")
        record["daily_change_pct"] = item.get("daily_change_pct")
        record["session_change_pct"] = item.get("session_change_pct")
        record["price_source"] = item.get("data_source") or item.get("price_source") or "Provider not reported"
        record["price_as_of"] = item.get("price_as_of") or now.isoformat()
        record["price_freshness"] = item.get("price_freshness") or "timestamp unavailable"
        record["price_observed_at"] = now.isoformat()
        record["peak_price"] = max(float(record.get("peak_price") or price), price)
        record["trough_price"] = min(float(record.get("trough_price") or price), price)
        first_seen = datetime.fromisoformat(record["first_seen"])
        age_days = max(0, (now - first_seen).total_seconds() / 86400)
        record["age_days"] = round(age_days, 2)
        record["return_pct"] = round((price / initial - 1) * 100, 2)
        record["max_gain_pct"] = round((float(record["peak_price"]) / initial - 1) * 100, 2)
        record["max_drawdown_pct"] = round((float(record["trough_price"]) / initial - 1) * 100, 2)
        stage = item.get("stage", item.get("signal", "WATCH"))
        record.setdefault("first_grade", stage)
        history = record.setdefault("grade_history", [])
        previous = record.get("last_stage")
        if not history: history.append({"from": None, "to": record["first_grade"], "at": record.get("first_seen", now.isoformat())})
        if previous and previous != stage: history.append({"from": previous, "to": stage, "at": now.isoformat()})
        record["grade_history"] = history[-100:]
        prices = record.setdefault("price_history", [])
        prices.append({"at": now.isoformat(), "price": price, "return_pct": record["return_pct"]})
        record["price_history"] = prices[-168:]
        reasons, catalysts = _history_context(item)
        record["discovery_reasons"] = reasons
        record["catalysts"] = catalysts
        record["risk_flags"] = (item.get("risk_flags") or [])[:8]
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
        record["last_stage"] = stage
        record["currently_surfaced"] = True
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


def update_buy_signal_history(candidates, now):
    """Persist distinct BUY events and measure forward returns from their signal prices."""
    try:
        state = json.loads(BUY_SIGNAL_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    signals = state.setdefault("signals", [])
    active = state.setdefault("active_by_key", {})
    by_id = {x.get("signal_id"): x for x in signals if x.get("signal_id")}
    horizons = {"1h": 3600, "4h": 14400, "1d": 86400, "3d": 259200, "7d": 604800}

    for item in candidates:
        asset, symbol = item.get("asset_type", "asset"), item.get("symbol", "unknown")
        key = f"{asset}:{symbol}"
        stage = item.get("stage", item.get("signal", "WATCH"))
        price = float(item.get("price") or 0)
        price_ok = price > 0 and not (asset == "stock" and not item.get("price_validation_passed", False))
        if stage != "BUY":
            signal_id = active.pop(key, None)
            old = by_id.get(signal_id)
            if old and old.get("status") == "active":
                old["status"], old["closed_at"] = "closed", now.isoformat()
            continue
        if not price_ok:
            continue

        signal_id = active.get(key)
        record = by_id.get(signal_id) if signal_id else None
        if record is None:
            levels = item.get("levels") or {}
            signal_id = f"{key}:{now.isoformat()}"
            record = {
                "signal_id": signal_id, "asset_type": asset, "symbol": symbol,
                "at": now.isoformat(), "entry_price": price,
                "entry_low": levels.get("watch_entry_low", price),
                "entry_high": levels.get("watch_entry_high", price),
                "invalidation": levels.get("invalidation_level"),
                "stop_loss": levels.get("stop_loss", levels.get("invalidation_level")),
                "target_1": levels.get("target_1"),
                "target_2": levels.get("target_2"),
                "risk_pct": levels.get("risk_pct"),
                "target_1_pct": levels.get("target_1_pct"),
                "target_2_pct": levels.get("target_2_pct"),
                "reward_risk_ratio": levels.get("reward_risk_ratio"),
                "target_2_reward_risk_ratio": levels.get("target_2_reward_risk_ratio"),
                "level_method": levels.get("method"),
                "score": int(item.get("score") or 0), "raw_score": item.get("raw_score"),
                "market_regime": item.get("market_regime"),
                "price_source": item.get("data_source") or item.get("price_source"),
                "price_as_of": item.get("price_as_of"),
                "reasons": (item.get("stage_reasons") or [])[:6],
                "risk_flags": (item.get("risk_flags") or [])[:8],
                "score_components": item.get("score_components") or {},
                "technical_analysis": item.get("technical_analysis") or {},
                "outcomes": {}, "latest_return_pct": 0.0,
                "max_favorable_pct": 0.0, "max_adverse_pct": 0.0, "status": "active",
            }
            signals.append(record)
            by_id[signal_id] = record
            active[key] = signal_id

        age = max(0, (now - datetime.fromisoformat(record["at"])).total_seconds())
        change = (price / float(record["entry_price"]) - 1) * 100
        record["latest_price"] = price
        record["latest_price_as_of"] = item.get("price_as_of")
        record["latest_observed_at"] = now.isoformat()
        record["latest_return_pct"] = round(change, 3)
        record["max_favorable_pct"] = round(max(float(record.get("max_favorable_pct") or 0), change), 3)
        record["max_adverse_pct"] = round(min(float(record.get("max_adverse_pct") or 0), change), 3)
        for horizon, seconds in horizons.items():
            if age >= seconds and horizon not in record["outcomes"]:
                record["outcomes"][horizon] = {
                    "at": now.isoformat(), "elapsed_seconds": int(age),
                    "price": price, "return_pct": round(change, 3),
                }

    signals.sort(key=lambda x: x.get("at", ""), reverse=True)
    state["signals"] = signals[:500]
    state["active_by_key"] = {
        key: sid for key, sid in active.items()
        if sid in by_id and by_id[sid].get("status") == "active"
    }
    BUY_SIGNAL_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state["signals"]


def main():
    if "--test" in sys.argv:
        email("Jarvis email test", "Jarvis successfully sent a test email. Email delivery is connected. This test does not confirm market scanning. Jarvis is recommendation-only and never places trades.")
        print("Test email sent.")
        return

    if not SCANNER_USERNAME or not SCANNER_PASSWORD:
        raise RuntimeError("Missing JARVIS_SITE_USERNAME or JARVIS_SITE_PASSWORD GitHub Actions secret.")
    credentials = base64.b64encode(f"{SCANNER_USERNAME}:{SCANNER_PASSWORD}".encode("utf-8")).decode("ascii")
    req = Request(BASE_URL + "/scan?asset=all", headers={
        "User-Agent": "JarvisAlertWorkflow/1.0",
        "Authorization": f"Basic {credentials}",
    })
    with urlopen(req, timeout=240) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload.get("candidates"), list):
        raise RuntimeError("Scanner response did not contain candidates.")

    DIAGNOSTICS_STATE.write_text(json.dumps({
        "timestamp": payload.get("timestamp"),
        "candidates_found": payload.get("candidates_found", 0),
        "diagnostics": payload.get("diagnostics") or {},
        "errors": payload.get("errors") or [],
        "market_conditions": payload.get("market_conditions") or {},
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo("America/Detroit"))
    paper, paper_lifetime = update_paper_tracker(payload["candidates"], now)
    buy_signals = update_buy_signal_history(payload["candidates"], now)
    # The GitHub workflow commits the tracker files after this run.
    # Render reads those committed files directly; no ephemeral website sync.
    # The history page reads the tracker JSON files committed by the workflow.
    # Do not POST to Render here: an optional sync must not fail the market scan.
    market = payload.get("market_conditions") or {}
    selected, updates = [], {}
    for item in payload["candidates"]:
        if item.get("asset_type") == "stock" and not item.get("price_validation_passed", False):
            continue
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
        is_buy = item.get("stage") == "BUY"
        new_buy = is_buy and previous.get("alerted_stage") != "BUY"
        if new_buy or (not is_buy and (not previous or expired or (score >= 75 and previous_score < 75) or score >= previous_score + 12)):
            selected.append(item)
            updates[key] = {"score": score, "alerted_at": now.isoformat(), "alerted_stage": item.get("stage")}

    # Continue scanning and updating paper performance around the clock, but
    # send email only during the preferred Eastern Time notification windows.
    weekday = local_now.weekday()  # Monday=0, Sunday=6
    local_hour = local_now.hour
    notification_window = ((weekday <= 4 and 7 <= local_hour < 21) or (weekday == 6 and 9 <= local_hour < 20))
    if not notification_window:
        print(f"Outside notification window ({local_now.isoformat()}); scan and paper tracking saved without email.")
        return

    if not selected:
        # Send one heartbeat per local Eastern calendar day.
        daily_key = local_now.date().isoformat()
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
            f"Lifetime tracked candidates: {paper_lifetime.get('tracked_candidates', paper_lifetime.get('recommendations', 0))}",
            "Lifetime checkpoint wins/observations: " + str({h: (v.get('wins', 0), v.get('observations', 0)) for h, v in paper_lifetime.get('checkpoints', {}).items()}),
            "Scan coverage: " + json.dumps(payload.get("diagnostics") or {}, sort_keys=True),
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

    has_buy_signal = any(item.get("stage") == "BUY" for item in selected)
    lines = [
        "JARVIS MARKET SCANNER — BUY SIGNALS" if has_buy_signal else "JARVIS MARKET SCANNER — NEW SETUPS",
        "Generated: " + str(payload.get("timestamp", now.isoformat())),
        "",
        "Research watchlist only. Not personalized financial advice. No trades are placed.",
        "Detailed paper history: rolling 365 days; lifetime checkpoint totals are retained.",
        f"Lifetime tracked candidates: {paper_lifetime.get('tracked_candidates', paper_lifetime.get('recommendations', 0))}",
        "Lifetime checkpoint wins/observations: " + str({h: (v.get('wins', 0), v.get('observations', 0)) for h, v in paper_lifetime.get('checkpoints', {}).items()}),
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
        if levels and item.get("stage") == "BUY":
            lines.append(f'ENTRY RANGE: {levels.get("watch_entry_low", "n/a")}–{levels.get("watch_entry_high", "n/a")}')
            lines.append(f'STOP-LOSS: {levels.get("stop_loss", levels.get("invalidation_level", "n/a"))} | Estimated downside from signal price: {levels.get("risk_pct", "n/a")}%')
            lines.append(f'POTENTIAL SELL: {levels.get("target_1", "n/a")} (+{levels.get("target_1_pct", "n/a")}%) | Stretch target: {levels.get("target_2", "n/a")} (+{levels.get("target_2_pct", "n/a")}%)')
            lines.append(f'REWARD/RISK: {levels.get("reward_risk_ratio", "n/a")}:1 to first target | {levels.get("target_2_reward_risk_ratio", "n/a")}:1 to stretch target')
            lines.append("Levels are illustrative, based on the scan price and current ATR/support; reassess if the price moves before entry.")
        elif levels:
            lines.append(f'Entry reference: {levels.get("watch_entry_low", "n/a")}–{levels.get("watch_entry_high", "n/a")} | Illustrative invalidation: {levels.get("invalidation_level", "n/a")}')
        if item.get("stage") == "BUY":
            lines.append("BUY SIGNAL: strict Jarvis paper recommendation. This is not an executed trade; user approval is required for any real order.")
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
    lines += [f'Scan candidates: {payload.get("candidates_found", "n/a")}', 'Scan diagnostics: ' + json.dumps(payload.get('diagnostics') or {}, sort_keys=True), f'Scan errors: {len(payload.get("errors") or [])}']
    buy_count = sum(1 for item in selected if item.get("stage") == "BUY")
    subject = (
        f"Jarvis BUY signal: {selected[0].get('symbol')}" if buy_count == 1 and len(selected) == 1
        else f"Jarvis: {buy_count} BUY signal(s), {len(selected)} new setup(s)" if buy_count
        else f"Jarvis: {len(selected)} new market setup(s)"
    )
    email(subject, "\n".join(lines))
    state.update(updates)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Sent {len(selected)} alert(s).")


if __name__ == "__main__":
    main()
