"""Research-only runner attribution and signal-quality summaries.

This module never changes scanner scores, BUY eligibility, or paper-trade execution.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, median
from typing import Any


MIN_COHORT_OBSERVATIONS = 8
MISSED_RUNNER_THRESHOLD_PCT = 20.0
MAX_RADAR_CANDIDATES = 30


def _number(value: Any):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _eligible_outcome_record(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if not record.get("early_runner_snapshot_at"):
        return False
    if not isinstance(record.get("early_runner_initial_feature_snapshot"), dict):
        return False
    if record.get("asset_type") == "stock":
        return record.get("session_aware_tracking") is True
    return record.get("asset_type") == "crypto"


def _official_buy_keys(signals):
    keys = set()
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        symbol = str(signal.get("symbol") or "").upper().strip()
        asset = str(signal.get("asset_type") or "").lower().strip()
        if symbol and asset in ("stock", "crypto"):
            keys.add((asset, symbol))
    return keys


def build_runner_intelligence_report(records, buy_signals=None):
    """Build transparent, sample-aware reports from saved discovery records."""
    values = [r for r in (records or []) if isinstance(r, dict)]
    buy_keys = _official_buy_keys(buy_signals)

    missed = []
    for record in values:
        asset = str(record.get("asset_type") or "").lower()
        symbol = str(record.get("symbol") or "").upper()
        max_gain = _number(record.get("discovery_max_gain_pct"))
        if asset not in ("stock", "crypto") or not symbol or max_gain is None:
            continue
        if str(record.get("discovery_return_status") or "").lower() != "verified":
            continue
        if max_gain < MISSED_RUNNER_THRESHOLD_PCT:
            continue
        official_buy_seen = (asset, symbol) in buy_keys
        if official_buy_seen:
            continue
        missed.append({
            "asset_type": asset,
            "symbol": symbol,
            "first_seen": record.get("first_seen"),
            "last_seen": record.get("last_seen"),
            "discovery_max_gain_pct": round(max_gain, 2),
            "discovery_return_pct": _number(record.get("discovery_return_pct")),
            "discovery_max_drawdown_pct": _number(record.get("discovery_max_drawdown_pct")),
            "last_stage": record.get("last_stage") or record.get("stage") or "WATCH",
            "price_validation_status": record.get("price_validation_status") or "unknown",
            "discovery_baseline_status": record.get("discovery_baseline_status") or "unknown",
            "official_buy_seen": False,
            "classification": "Watchlist runner; no matching official BUY record found",
        })
    missed.sort(key=lambda row: row["discovery_max_gain_pct"], reverse=True)

    eligible = [r for r in values if _eligible_outcome_record(r)]
    cohort_members = defaultdict(list)
    for record in eligible:
        outcome = (record.get("early_runner_outcomes") or {}).get("5d")
        ret = _number(outcome.get("return_pct")) if isinstance(outcome, dict) else None
        if ret is None:
            continue
        features = record.get("early_runner_initial_feature_snapshot") or {}
        labels = record.get("early_runner_initial_signal_matches") or features.get("early_warning_matches") or []
        if isinstance(labels, str):
            labels = [labels]
        for label in sorted({str(x).strip() for x in labels if str(x).strip()}):
            cohort_members[(record.get("asset_type"), label)].append((record, ret))

    cohorts = []
    for (asset, label), samples in cohort_members.items():
        returns = [ret for _, ret in samples]
        runners = sum(ret >= MISSED_RUNNER_THRESHOLD_PCT for ret in returns)
        mfe = [_number(r.get("research_max_favorable_pct")) for r, _ in samples]
        mae = [_number(r.get("research_max_adverse_pct")) for r, _ in samples]
        mfe = [x for x in mfe if x is not None]
        mae = [x for x in mae if x is not None]
        count = len(returns)
        cohorts.append({
            "asset_type": asset,
            "setup": label,
            "observations": count,
            "runner_count": runners,
            "runner_rate_pct": round(runners / count * 100, 1),
            "average_5d_return_pct": round(mean(returns), 2),
            "median_5d_return_pct": round(median(returns), 2),
            "average_mfe_pct": round(mean(mfe), 2) if mfe else None,
            "average_mae_pct": round(mean(mae), 2) if mae else None,
            "sample_status": "usable for directional review" if count >= MIN_COHORT_OBSERVATIONS else "early sample · do not promote",
            "minimum_observations_for_review": MIN_COHORT_OBSERVATIONS,
            "runner_definition": "5-day sampled return >=20%",
        })
    cohorts.sort(key=lambda row: (row["observations"] >= MIN_COHORT_OBSERVATIONS,
                                  row["runner_rate_pct"], row["observations"]), reverse=True)

    radar = []
    for record in values:
        if record.get("currently_surfaced") is False:
            continue
        asset = str(record.get("asset_type") or "").lower()
        symbol = str(record.get("symbol") or "").upper()
        if asset not in ("stock", "crypto") or not symbol:
            continue
        score = _number(record.get("early_runner_research_score"))
        matches = record.get("early_runner_signal_matches") or record.get("research_signal_matches") or []
        if isinstance(matches, str):
            matches = [matches]
        if score is None and not matches:
            continue
        radar.append({
            "asset_type": asset,
            "symbol": symbol,
            "research_score": score,
            "matches": [str(x) for x in matches[:8]],
            "stage": record.get("last_stage") or record.get("stage") or "WATCH",
            "price_validation_status": record.get("price_validation_status") or "unknown",
            "latest_price": _number(record.get("latest_price")),
            "price_as_of": record.get("price_as_of"),
            "breakout_proximity_pct": _number(record.get("breakout_proximity_pct")),
            "premarket_rvol_same_time": _number(record.get("premarket_rvol_same_time")),
            "relative_strength_vs_spy": _number(record.get("relative_strength_vs_spy")),
            "accumulation_signal_score": _number(record.get("accumulation_signal_score")),
            "multi_timeframe_status": record.get("multi_timeframe_status"),
            "dilution_flags": record.get("dilution_flags") or [],
            "notice": "Research-only watch candidate; not an official BUY signal.",
        })
    radar.sort(key=lambda row: (row["research_score"] if row["research_score"] is not None else -1,
                                len(row["matches"])), reverse=True)

    return {
        "missed_runner_tracker": {
            "threshold_pct": MISSED_RUNNER_THRESHOLD_PCT,
            "verified_watchlist_runners": len(missed),
            "candidates": missed[:50],
            "definition": "Verified discovery baseline with at least +20% observed maximum gain and no matching official BUY record in retained signal history. This is a watchlist miss indicator, not proof a safe entry was available.",
            "excluded_unverified": True,
        },
        "signal_quality_by_setup": {
            "minimum_observations": MIN_COHORT_OBSERVATIONS,
            "cohorts": cohorts,
            "definition": "Completed 5-day forward observations from frozen, verified feature snapshots. Runner means sampled return >=20%; small cohorts are explicitly provisional.",
        },
        "early_warning_radar": {
            "candidate_count": len(radar),
            "candidates": radar[:MAX_RADAR_CANDIDATES],
            "notice": "Research-only candidates. Price verification and all official BUY gates remain separate and unchanged.",
        },
    }
