import json
import math
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


def email(subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, RECIPIENT, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(SENDER, PASSWORD)
        smtp.send_message(msg)


def sync_history_to_site(paper, lifetime, now, buy_signals=None, scan_metadata=None):
    token = os.getenv("JARVIS_HISTORY_SYNC_TOKEN", "")
    if not token:
        print("History sync skipped: JARVIS_HISTORY_SYNC_TOKEN is not configured.")
        return
    scan_metadata = scan_metadata or {}
    body = json.dumps({
        "synced_at": now.isoformat(),
        "last_scan_completed_at": scan_metadata.get("last_scan_completed_at"),
        "scan_run_id": scan_metadata.get("run_id"),
        "paper_tracker": paper,
        "lifetime": lifetime,
        "buy_signals": buy_signals or [],
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


def safe_float(value, default=0.0):
    """Convert optional market-data values safely; reject NaN and infinity."""
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


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

    # One-time migration for lifetime recommendation accounting.
    for existing in tracker.values():
        if "lifetime_recommendation_counted" not in existing:
            has_baseline = existing.get("initial_price") is not None
            was_unverified_promotion = (
                existing.get("asset_type") == "stock"
                and has_baseline
                and "quarantined_initial_price" in existing
                and existing.get("quarantined_initial_price") is None
            )
            if was_unverified_promotion:
                lifetime["recommendations"] = int(lifetime.get("recommendations", 0)) + 1
                asset_stats = lifetime.setdefault("by_asset", {}).setdefault(
                    "stock", {"recommendations": 0}
                )
                asset_stats["recommendations"] = int(asset_stats.get("recommendations", 0)) + 1
                existing["lifetime_recommendation_counted"] = True
            else:
                existing["lifetime_recommendation_counted"] = bool(has_baseline)

    # Mark prior records absent until they are rediscovered in this scan.
    for existing in tracker.values():
        existing["currently_surfaced"] = False
        if existing.get("asset_type") in ("stock", "crypto") and existing.get("price_validation_status") in (None, "not_required"):
            existing["price_validation_status"] = "unverified"
            existing["price_validation_note"] = "Legacy price has no retained exchange timestamp validation evidence"
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
        if asset_type in ("stock", "crypto") and not item.get("price_validation_passed", False):
            if not record:
                # Keep an audit row for newly discovered but unverified stocks.
                # This is not counted as a paper recommendation until a price is accepted.
                record = {
                    "asset_type": asset_type, "symbol": symbol,
                    "first_seen": now.isoformat(), "initial_price": None,
                    "peak_price": None, "trough_price": None, "milestones": {},
                    "price_history": [], "discovery_reasons": [], "catalysts": [],
                    "risk_flags": [], "currently_surfaced": True,
                    "lifetime_recommendation_counted": False,
                }
            record["price_validation_status"] = "unverified"
            record["price_validation_note"] = item.get("price_validation_note", "Exchange price validation unavailable")
            record["price_validation_method"] = item.get("price_validation_method")
            record["price_age_seconds"] = item.get("price_age_seconds")
            record["price_cross_check_difference_pct"] = item.get("price_cross_check_difference_pct")
            record["webull_status"] = item.get("webull_status", "not_reported")
            record["webull_price"] = item.get("webull_price")
            record["webull_price_as_of"] = item.get("webull_price_as_of")
            record["webull_price_age_minutes"] = item.get("webull_price_age_minutes")
            record["webull_price_difference_pct"] = item.get("webull_price_difference_pct")
            record["massive_quote_status"] = item.get("massive_quote_status", "not_reported")
            record["massive_price"] = item.get("massive_price")
            record["massive_price_as_of"] = item.get("massive_price_as_of")
            record["massive_price_age_minutes"] = item.get("massive_price_age_minutes")
            record["market_session"] = item.get("market_session")
            record["price_source"] = item.get("data_source") or item.get("price_source")
            record["price_as_of"] = item.get("price_as_of")
            record["price_freshness"] = item.get("price_freshness")
            record["data_quality"] = item.get("data_quality") or {}
            record["daily_data_quality"] = item.get("daily_data_quality") or {}
            record["intraday_data_quality"] = item.get("intraday_data_quality") or {}
            record["hourly_data_quality"] = item.get("hourly_data_quality") or {}
            record["last_stage"] = item.get("stage", item.get("signal", "WATCH"))
            record["potential_stage"] = item.get("potential_stage")
            record["score"] = int(item.get("score") or 0)
            record["raw_score"] = item.get("raw_score")
            record["daily_change_pct"] = item.get("daily_change_pct")
            record["five_day_change_pct"] = item.get("five_day_change_pct")
            record["stage_reasons"] = (item.get("stage_reasons") or [])[:10]
            record["stage_signals"] = item.get("stage_signals") or {}
            record["technical_analysis"] = item.get("technical_analysis") or {}
            record["score_components"] = item.get("score_components") or {}
            record["last_seen"] = now.isoformat()
            record["unverified_return_pct"] = record.get("return_pct")
            record["return_pct"] = None
            record["max_gain_pct"] = None
            record["max_drawdown_pct"] = None
            record["latest_price"] = None
            record["currently_surfaced"] = True
            research_fields = (
                "relative_strength_vs_spy", "relative_strength_vs_iwm", "rs_rank",
                "intraday_rvol_same_time", "same_time_relative_volume",
                "anchored_vwap", "anchored_vwap_status", "opening_range_high", "opening_range_low",
                "opening_range_status", "adx", "plus_di", "minus_di", "adx_dmi_status",
                "spread_pct", "dollar_volume", "liquidity_status",
                "volatility_contraction_status", "atr_contraction_pct", "base_tightness_pct",
                "premarket_gap_pct", "premarket_volume", "premarket_rvol_same_time",
                "premarket_high", "premarket_low", "cumulative_rvol_same_time",
                "volume_acceleration_3bar", "float_rotation_pct", "float_shares",
                "shares_outstanding", "short_percent_float", "higher_highs_higher_lows",
                "pullback_from_session_high_pct", "premarket_high_reclaimed",
                "catalyst_labels", "catalyst_dilution_status", "dilution_flags",
                "catalyst_headline_count", "early_runner_research_score", "early_runner_score_components",
                "crqs", "crqs_tier", "crqs_components", "crqs_data_coverage", "crqs_entry_score", "crqs_entry_penalty", "crqs_features", "crqs_relative_strength_vs_btc_24h_pct", "crqs_sector_activity", "crqs_sector_rank", "crqs_sector_count", "crypto_sector", "buy_readiness",
                "data_quality", "daily_data_quality", "intraday_data_quality", "hourly_data_quality",
            )
            for field in research_fields:
                record[field] = item.get(field)
            record["research_feature_snapshot"] = {field: item.get(field) for field in research_fields}
            record["research_signal_matches"] = item.get("research_signal_matches") or []
            record["buy_readiness"] = item.get("buy_readiness") or {}
            record["early_runner_signal_matches"] = item.get("early_runner_signal_matches") or []
            record["early_runner_research_score"] = item.get("early_runner_research_score")
            tracker[key] = record
            continue
        if record and record.get("price_validation_status") == "unverified":
            if record.get("initial_price") is None and not record.get("lifetime_recommendation_counted", False):
                lifetime["recommendations"] = int(lifetime.get("recommendations", 0)) + 1
                asset_stats = lifetime.setdefault("by_asset", {}).setdefault(
                    asset_type, {"recommendations": 0}
                )
                asset_stats["recommendations"] = int(asset_stats.get("recommendations", 0)) + 1
                record["lifetime_recommendation_counted"] = True
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
            record["price_validation_note"] = item.get("price_validation_note") or "Baseline reset after exchange timestamp validation"
            record["price_validation_method"] = item.get("price_validation_method")
            record["price_age_seconds"] = item.get("price_age_seconds")
            record["price_cross_check_difference_pct"] = item.get("price_cross_check_difference_pct")
        if not record:
            record = {
                "asset_type": asset_type, "symbol": symbol,
                "first_seen": now.isoformat(), "initial_price": price,
                "peak_price": price, "trough_price": price, "milestones": {},
                "first_grade": item.get("stage", item.get("signal", "WATCH")),
                "grade_history": [{"from": None, "to": item.get("stage", item.get("signal", "WATCH")), "at": now.isoformat()}],
                "price_history": [], "discovery_reasons": [], "catalysts": [], "risk_flags": [],
                "lifetime_recommendation_counted": True,
            }
            lifetime["recommendations"] = int(lifetime.get("recommendations", 0)) + 1
            asset_stats = lifetime.setdefault("by_asset", {}).setdefault(asset_type, {"recommendations": 0})
            asset_stats["recommendations"] += 1

        initial = float(record.get("initial_price") or price)
        record["latest_price"] = price
        record["price_validation_status"] = "verified" if item.get("price_validation_passed", False) else "unverified"
        record["price_validation_note"] = item.get("price_validation_note", "Price validation evidence unavailable")
        record["price_validation_method"] = item.get("price_validation_method")
        record["price_age_seconds"] = item.get("price_age_seconds")
        record["price_cross_check_difference_pct"] = item.get("price_cross_check_difference_pct")
        record["price_as_of"] = item.get("price_as_of")
        record["price_freshness"] = item.get("price_freshness")
        record["data_quality"] = item.get("data_quality") or {}
        record["daily_data_quality"] = item.get("daily_data_quality") or {}
        record["intraday_data_quality"] = item.get("intraday_data_quality") or {}
        record["hourly_data_quality"] = item.get("hourly_data_quality") or {}
        record["webull_status"] = item.get("webull_status", "not_reported")
        record["webull_price"] = item.get("webull_price")
        record["webull_price_as_of"] = item.get("webull_price_as_of")
        record["webull_price_age_minutes"] = item.get("webull_price_age_minutes")
        record["webull_price_difference_pct"] = item.get("webull_price_difference_pct")
        record["massive_quote_status"] = item.get("massive_quote_status", "not_reported")
        record["massive_price"] = item.get("massive_price")
        record["massive_price_as_of"] = item.get("massive_price_as_of")
        record["massive_price_age_minutes"] = item.get("massive_price_age_minutes")
        record["market_session"] = item.get("market_session")
        record["daily_change_pct"] = item.get("daily_change_pct")
        record["session_change_pct"] = item.get("session_change_pct")
        record["price_source"] = item.get("data_source") or item.get("price_source") or "Provider not reported"
        # Preserve the hypothetical plan and research attribution for the Research tab.
        # These fields are observational and never affect BUY scoring.
        record["levels"] = item.get("levels") or {}
        record["score"] = int(item.get("score") or 0)
        record["raw_score"] = item.get("raw_score")
        record["market_regime"] = item.get("market_regime")
        record["stage_signals"] = item.get("stage_signals") or {}
        record["price_validation_note"] = item.get("price_validation_note")
        record["stage_reasons"] = (item.get("stage_reasons") or [])[:10]
        record["score_components"] = item.get("score_components") or {}
        record["technical_analysis"] = item.get("technical_analysis") or {}
        research_fields = (
            "relative_strength_vs_spy", "relative_strength_vs_iwm", "rs_rank",
            "intraday_rvol_same_time", "same_time_relative_volume",
            "anchored_vwap", "anchored_vwap_status", "opening_range_status",
            "opening_range_high", "opening_range_low", "adx", "plus_di", "minus_di",
            "adx_dmi_status", "spread_pct", "dollar_volume", "liquidity_status",
            "volatility_contraction_status", "atr_contraction_pct", "base_tightness_pct",
                "premarket_gap_pct", "premarket_volume", "premarket_rvol_same_time",
                "premarket_high", "premarket_low", "cumulative_rvol_same_time",
                "volume_acceleration_3bar", "float_rotation_pct", "float_shares",
                "shares_outstanding", "short_percent_float", "higher_highs_higher_lows",
                "pullback_from_session_high_pct", "premarket_high_reclaimed",
                "catalyst_labels", "catalyst_dilution_status", "dilution_flags",
                "catalyst_headline_count", "early_runner_research_score", "early_runner_score_components", "buy_readiness",
                "crqs", "crqs_tier", "crqs_components", "crqs_data_coverage", "crqs_entry_score", "crqs_entry_penalty", "crqs_features",
        )
        for field in research_fields:
            record[field] = item.get(field)
        record["research_signal_matches"] = item.get("research_signal_matches") or item.get("research_signals") or []
        record["early_runner_signal_matches"] = item.get("early_runner_signal_matches") or []
        record["early_runner_research_score"] = item.get("early_runner_research_score")
        record["research_feature_snapshot"] = {field: item.get(field) for field in research_fields}
        # Freeze a separate feature snapshot at the start of this research cohort.
        # Later scans may update live fields but must not rewrite the features used for outcome attribution.
        if not record.get("early_runner_snapshot_at"):
            record["early_runner_snapshot_at"] = now.isoformat()
            record["early_runner_entry_price"] = price
            record["early_runner_initial_feature_snapshot"] = dict(record["research_feature_snapshot"])
            record["early_runner_initial_signal_matches"] = list(item.get("early_runner_signal_matches") or [])
            record["early_runner_outcomes"] = {}
        early_entry = safe_float(record.get("early_runner_entry_price"))
        early_started = datetime.fromisoformat(record["early_runner_snapshot_at"])
        early_age = max(0, (now - early_started).total_seconds())
        record.setdefault("early_runner_outcomes", {})
        for horizon, seconds in {"1h": 3600, "4h": 14400, "1d": 86400, "3d": 259200, "5d": 432000}.items():
            if early_age >= seconds and horizon not in record["early_runner_outcomes"] and early_entry > 0:
                record["early_runner_outcomes"][horizon] = {
                    "at": now.isoformat(), "elapsed_seconds": int(early_age),
                    "price": price, "return_pct": round((price / early_entry - 1) * 100, 3),
                    "sampling_note": "First completed scan after feature snapshot at or beyond target horizon; not an exact-time fill"
                }
        record["research_tracking_started_at"] = record.get("research_tracking_started_at") or record.get("first_seen") or now.isoformat()
        research_entry = float(record.get("research_entry_price") or record.get("initial_price") or price)
        record["research_entry_price"] = research_entry
        sample_high = float(item.get("research_sample_high") or price)
        sample_low = float(item.get("research_sample_low") or price)
        record["research_peak_price"] = max(float(record.get("research_peak_price") or research_entry), sample_high, price)
        record["research_trough_price"] = min(float(record.get("research_trough_price") or research_entry), sample_low, price)
        if research_entry > 0:
            record["research_max_favorable_pct"] = round((record["research_peak_price"] / research_entry - 1) * 100, 3)
            record["research_max_adverse_pct"] = round((record["research_trough_price"] / research_entry - 1) * 100, 3)
        research_age = max(0, (now - datetime.fromisoformat(record["research_tracking_started_at"])).total_seconds())
        record.setdefault("research_outcomes", {})
        research_horizons = {"1h": 3600, "4h": 14400, "24h": 86400, "72h": 259200, "5d": 432000, "10d": 864000}
        for horizon, seconds in research_horizons.items():
            if research_age >= seconds and horizon not in record["research_outcomes"] and research_entry > 0:
                record["research_outcomes"][horizon] = {
                    "at": now.isoformat(), "elapsed_seconds": int(research_age),
                    "price": price, "return_pct": round((price / research_entry - 1) * 100, 3),
                    "sampling_note": "First completed scan at or after the target horizon; not an exact-time fill"
                }
        # Track false breakouts conservatively: a later completed scan must close
        # below the recorded breakout level by at least 0.5%.
        opening_status = str(item.get("opening_range_status") or "").lower()
        breakout_now = bool(item.get("breakout_20d") or "15m upside breakout" in opening_status)
        if breakout_now and not record.get("research_breakout_at"):
            breakout_level = float(item.get("opening_range_high") or item.get("resistance_20d") or price)
            record["research_breakout_at"] = now.isoformat()
            record["research_breakout_level"] = breakout_level
            record["research_breakout_status"] = "pending"
        elif record.get("research_breakout_status") in ("pending", "held") and record.get("research_breakout_at") != now.isoformat():
            breakout_level = float(record.get("research_breakout_level") or 0)
            if breakout_level > 0 and price < breakout_level * 0.995:
                record["research_breakout_status"] = "failed"
                record["research_false_breakout_at"] = now.isoformat()
            elif breakout_level > 0 and price >= breakout_level:
                record["research_breakout_status"] = "held"
        record["research_setup_status"] = item.get("research_setup_status")
        record["discovery_source"] = "research_layer" if record["research_signal_matches"] else "core_scanner"
        record["price_as_of"] = item.get("price_as_of") or now.isoformat()
        record["price_freshness"] = item.get("price_freshness") or "timestamp unavailable"
        record["data_quality"] = item.get("data_quality") or {}
        record["daily_data_quality"] = item.get("daily_data_quality") or {}
        record["intraday_data_quality"] = item.get("intraday_data_quality") or {}
        record["hourly_data_quality"] = item.get("hourly_data_quality") or {}
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
        # Persist the full research feature snapshot for verified/normal records too.
        research_fields = (
            "relative_strength_vs_spy", "relative_strength_vs_iwm", "rs_rank",
            "intraday_rvol_same_time", "same_time_relative_volume",
            "anchored_vwap", "anchored_vwap_status", "opening_range_high", "opening_range_low",
            "opening_range_status", "adx", "plus_di", "minus_di", "adx_dmi_status",
            "spread_pct", "dollar_volume", "liquidity_status",
            "volatility_contraction_status", "atr_contraction_pct", "base_tightness_pct",
            "premarket_gap_pct", "premarket_volume", "premarket_rvol_same_time",
            "premarket_high", "premarket_low", "cumulative_rvol_same_time",
            "volume_acceleration_3bar", "float_rotation_pct", "float_shares",
            "shares_outstanding", "short_percent_float", "higher_highs_higher_lows",
            "pullback_from_session_high_pct", "premarket_high_reclaimed",
            "catalyst_labels", "catalyst_dilution_status", "dilution_flags",
            "catalyst_headline_count", "early_runner_research_score", "early_runner_score_components",
            "crqs", "crqs_tier", "crqs_components", "crqs_data_coverage", "crqs_entry_score", "crqs_entry_penalty", "crqs_features", "crqs_relative_strength_vs_btc_24h_pct", "crqs_sector_activity", "crqs_sector_rank", "crqs_sector_count", "crypto_sector", "buy_readiness",
                "data_quality", "daily_data_quality", "intraday_data_quality", "hourly_data_quality",
        )
        for field in research_fields:
            record[field] = item.get(field)
        record["research_feature_snapshot"] = {field: item.get(field) for field in research_fields}
        record["research_signal_matches"] = item.get("research_signal_matches") or []
        record["buy_readiness"] = item.get("buy_readiness") or {}
        record["early_runner_signal_matches"] = item.get("early_runner_signal_matches") or []
        record["early_runner_research_score"] = item.get("early_runner_research_score")
        record["last_seen"] = now.isoformat()
        record["last_stage"] = stage
        record["currently_surfaced"] = True
        tracker[key] = record
        summaries[key] = record

    # Persist a per-record reconciliation without inventing outcome checkpoints.
    retained_stock = [r for r in tracker.values() if r.get("asset_type") == "stock"]
    stock_baselines = [r for r in retained_stock if r.get("initial_price") is not None]
    uncounted_baselines = [r for r in stock_baselines if not r.get("lifetime_recommendation_counted", False)]
    lifetime["integrity"] = {
        "checked_at": now.isoformat(),
        "retained_stock_baselines": len(stock_baselines),
        "retained_stock_counted_baselines": len(stock_baselines) - len(uncounted_baselines),
        "retained_stock_uncounted_baselines": len(uncounted_baselines),
        "unverified_stock_without_baseline": sum(
            1 for r in retained_stock
            if r.get("price_validation_status") == "unverified" and r.get("initial_price") is None
        ),
    }

    # Keep one year of detailed recommendations; lifetime aggregates remain cumulative.
    cutoff = now - timedelta(days=365)
    tracker = {
        key: value for key, value in tracker.items()
        if datetime.fromisoformat(value.get("last_seen", value.get("first_seen"))) >= cutoff
    }
    PAPER_STATE.write_text(json.dumps(tracker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PAPER_LIFETIME_STATE.write_text(json.dumps(lifetime, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The live dashboard needs the complete retained ledger, including unverified
    # watch-only candidates and candidates absent from the latest scan.
    return tracker, lifetime


def update_buy_signal_history(candidates, now, scan_errors=None):
    """Persist BUY events, sampled stop/target observations, and forward returns."""
    try:
        state = json.loads(BUY_SIGNAL_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    signals = state.setdefault("signals", [])
    active = state.setdefault("active_by_key", {})
    by_id = {x.get("signal_id"): x for x in signals if x.get("signal_id")}
    horizons = {"1h": 3600, "4h": 14400, "1d": 86400, "3d": 259200, "7d": 604800}
    present_keys = set()

    for item in candidates:
        asset, symbol = item.get("asset_type", "asset"), item.get("symbol", "unknown")
        key = f"{asset}:{symbol}"
        present_keys.add(key)
        stage = item.get("stage", item.get("signal", "WATCH"))
        price = float(item.get("price") or 0)
        price_ok = price > 0 and (asset not in ("stock", "crypto") or item.get("price_validation_passed", False))
        if not price_ok:
            continue

        # Continue measuring previously issued BUY signals even after they are
        # closed or no longer meet today's BUY filter. A signal outcome belongs
        # to the original signal timestamp, not to how long it stays active.
        for prior in signals:
            if prior.get("asset_type") != asset or prior.get("symbol") != symbol:
                continue
            try:
                prior_age = max(0, (now - datetime.fromisoformat(prior["at"])).total_seconds())
                prior_entry = float(prior.get("entry_price") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if prior_entry <= 0:
                continue
            prior.setdefault("outcomes", {})
            prior_change = (price / prior_entry - 1) * 100
            for horizon, seconds in horizons.items():
                if prior_age >= seconds and horizon not in prior["outcomes"]:
                    prior["outcomes"][horizon] = {
                        "at": now.isoformat(), "elapsed_seconds": int(prior_age),
                        "price": price, "return_pct": round(prior_change, 3),
                        "sampling_note": "First completed scan at or after the target horizon; not an exact-time fill"
                    }

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
                "research_signal_matches": item.get("research_signal_matches") or item.get("research_signals") or [],
                "discovery_source": "research_layer" if (item.get("research_signal_matches") or item.get("research_signals")) else "core_scanner",
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
        # Prices are sampled scan observations, not guaranteed intrabar fills.
        # Record first observed crossings without claiming an exact execution.
        record.setdefault("level_observations", {})
        stop = safe_float(record.get("stop_loss"))
        target_1 = safe_float(record.get("target_1"))
        target_2 = safe_float(record.get("target_2"))
        if stop > 0 and price <= stop and "stop_observed" not in record["level_observations"]:
            record["level_observations"]["stop_observed"] = {
                "at": now.isoformat(), "observed_price": price
            }
        if target_1 > 0 and price >= target_1 and "target_1_observed" not in record["level_observations"]:
            record["level_observations"]["target_1_observed"] = {
                "at": now.isoformat(), "observed_price": price
            }
        if target_2 > 0 and price >= target_2 and "target_2_observed" not in record["level_observations"]:
            record["level_observations"]["target_2_observed"] = {
                "at": now.isoformat(), "observed_price": price
            }
        record["clean_scans_missing"] = 0
        for horizon, seconds in horizons.items():
            if age >= seconds and horizon not in record["outcomes"]:
                record["outcomes"][horizon] = {
                    "at": now.isoformat(), "elapsed_seconds": int(age),
                    "price": price, "return_pct": round(change, 3),
                }

    # If a previously active BUY disappears from six consecutive clean scans,
    # close it as stale. Any scan errors pause this rule to avoid false closures.
    if not scan_errors:
        for key, signal_id in list(active.items()):
            if key in present_keys:
                continue
            record = by_id.get(signal_id)
            if not record or record.get("status") != "active":
                active.pop(key, None)
                continue
            record["clean_scans_missing"] = int(record.get("clean_scans_missing") or 0) + 1
            if record["clean_scans_missing"] >= 6:
                record["status"] = "stale"
                record["closed_at"] = now.isoformat()
                record["closure_reason"] = "Absent from six consecutive completed scans"
                active.pop(key, None)

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

    metadata_path = Path("scan_metadata.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    metadata.update({
        "last_scan_started_at": datetime.now(timezone.utc).isoformat(),
        "last_attempt_status": "running",
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "run_number": os.getenv("GITHUB_RUN_NUMBER"),
        "commit_sha": os.getenv("GITHUB_SHA"),
        "execution_environment": "github_actions_runner",
    })
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    # Execute the scanner in this runner process so GitHub Actions secrets,
    # including Alpaca credentials, are available to the actual scan workers.
    # The previous implementation called Render's /scan endpoint, where these
    # runner-only secrets were not present.
    alpaca_configured = bool(
        os.getenv("ALPACA_API_KEY", "").strip()
        and os.getenv("ALPACA_API_SECRET", "").strip()
    )
    print(f"Alpaca credentials available to scanner: {'yes' if alpaca_configured else 'no'}")
    from main import scan as run_market_scan
    payload = run_market_scan(asset="all")

    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise RuntimeError("Scanner response did not contain candidates.")
    completed_at = datetime.now(timezone.utc).isoformat()
    metadata.update({
        "last_scan_completed_at": completed_at,
        "last_attempt_status": "completed",
        "last_attempt_error": None,
        "scan_duration_seconds": payload.get("diagnostics", {}).get("scan_duration_seconds"),
        "scan_errors": len(payload.get("errors") or []),
    })
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

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
    buy_signals = update_buy_signal_history(payload["candidates"], now, payload.get("errors") or [])
    # Keep the live history page current without waiting for a Render redeploy.
    # The committed JSON files remain the recovery source if the site restarts.
    try:
        sync_history_to_site(paper, paper_lifetime, now, buy_signals, metadata)
    except Exception as exc:
        print(f"History sync failed; committed tracker files remain available: {type(exc).__name__}: {exc}")
    market = payload.get("market_conditions") or {}
    selected, updates = [], {}
    for item in payload["candidates"]:
        # Research alerts may use delayed/EOD/historical data, but their
        # freshness label is included below. BUY stage is independently gated.
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
            f'PRICE QUALITY: {(item.get("data_quality") or {}).get("data_type", "unknown").upper()} | Validation: {item.get("price_validation_status", "unknown")} | Entry: {item.get("entry_status", "RESEARCH ONLY")}',
        ]
        levels = item.get("levels") or {}
        if levels and item.get("stage") == "BUY":
            lines.append(f'ENTRY RANGE: {levels.get("watch_entry_low", "n/a")}–{levels.get("watch_entry_high", "n/a")}')
            lines.append(f'STOP-LOSS: {levels.get("stop_loss", levels.get("invalidation_level", "n/a"))} | Estimated risk from upper entry reference: {levels.get("risk_pct", "n/a")}%')
            lines.append(f'POTENTIAL SELL: {levels.get("target_1", "n/a")} (+{levels.get("target_1_pct", "n/a")}%) | Stretch target: {levels.get("target_2", "n/a")} (+{levels.get("target_2_pct", "n/a")}%)')
            lines.append(f'REWARD/RISK: {levels.get("reward_risk_ratio", "n/a")}:1 to first target | {levels.get("target_2_reward_risk_ratio", "n/a")}:1 to stretch target')
            lines.append("Levels are illustrative; risk and targets use the upper entry reference. Stop/target events are recorded only when a scan observes a crossing, not as confirmed fills.")
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
