"""Bounded, shadow-only per-scan snapshots for early opportunity research.

This module records evidence and labels research stages. It never changes official
scores, BUY eligibility, alerts, paper-trade entries, or execution permissions.
"""
from __future__ import annotations

import math

MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE = 32

FEATURE_FIELDS = (
    "early_runner_research_score", "volume_acceleration_3bar",
    "cumulative_rvol_same_time", "premarket_rvol_same_time",
    "float_rotation_pct", "higher_highs_higher_lows", "higher_lows",
    "relative_volume", "intraday_relative_volume", "change_1h_pct",
    "change_24h_pct", "breakout_proximity_pct", "breakout_20d",
    "breakout_24h", "intraday_breakout", "accumulation_signal_score",
    "volatility_compression_percentile", "liquidity_sweep_detected",
    "multi_timeframe_alignment", "weekly_confirmation",
    "weekly_breakout_confluence", "relative_strength_vs_spy",
    "relative_strength_vs_iwm", "crqs", "crqs_tier",
)

def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None

def _list(value, limit=8):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x)[:120] for x in value if x is not None and str(x).strip()][:limit]

def _classify(item):
    asset = str(item.get("asset_type") or "").lower()
    if asset not in ("stock", "crypto"):
        return None, []
    evidence = []
    for field in ("early_warning_matches", "early_runner_signal_matches", "research_signal_matches"):
        evidence.extend(_list(item.get(field), 8))
    evidence = list(dict.fromkeys(evidence))[:12]
    proximity = _number(item.get("breakout_proximity_pct"))
    breakout = bool(item.get("breakout_20d") or item.get("intraday_breakout") or item.get("breakout_24h"))
    accumulation = _number(item.get("accumulation_signal_score"))
    early_score = _number(item.get("early_runner_research_score"))
    volume_accel = _number(item.get("volume_acceleration_3bar"))
    cumulative_rvol = _number(item.get("cumulative_rvol_same_time"))
    relative_volume = _number(item.get("relative_volume"))
    hourly_change = _number(item.get("change_1h_pct"))
    readiness = _number((item.get("buy_readiness") or {}).get("percent"))
    stage = str(item.get("stage") or item.get("signal") or "").upper()

    if breakout:
        label = "BREAKOUT DEVELOPING"
        evidence.append("Existing breakout flag")
    elif proximity is not None and -0.25 <= proximity <= 3.0:
        label = "PRE-BREAKOUT WATCH"
        evidence.append("Within 3% of recorded breakout reference")
    elif (
        (accumulation is not None and accumulation >= 50)
        or (early_score is not None and early_score >= 40)
        or (volume_accel is not None and volume_accel >= 1.5
            and cumulative_rvol is not None and cumulative_rvol >= 1.3)
        or (asset == "crypto" and item.get("higher_lows") is True
            and relative_volume is not None and relative_volume >= 1.5
            and hourly_change is not None and hourly_change > 0)
    ):
        label = "EARLY ACCUMULATION"
    elif evidence or (readiness is not None and readiness >= 30) or (
        _number(item.get("score")) is not None and _number(item.get("score")) >= 45
    ) or stage in {"EARLY WATCH", "EARLY SETUP", "DEVELOPING SETUP", "DEVELOPING RUNNER", "ACTIONABLE SETUP"}:
        label = "EARLY WATCH"
    else:
        return None, evidence
    return label, list(dict.fromkeys(evidence))[:12]

def build_candidate_snapshot(item, observed_at, scan_id):
    """Return a compact candidate snapshot, or None when no early evidence exists."""
    if not isinstance(item, dict):
        return None
    label, evidence = _classify(item)
    if label is None:
        return None
    asset = str(item.get("asset_type") or "").lower()
    status = str(item.get("price_validation_status") or "unknown").lower()
    verified = bool(item.get("price_validation_passed")) or status == "verified"
    data_quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
    data_type = str(data_quality.get("data_type") or "unknown").lower()
    age = _number(data_quality.get("age_seconds"))
    session = str(data_quality.get("session") or "").upper()
    outcome_eligible = verified and (
        asset == "crypto" or (
            data_type == "live" and (age is None or age <= 900)
            and session in {"REGULAR SESSION", "PRE-MARKET", "AFTER-HOURS"}
        )
    )
    readiness = item.get("buy_readiness") if isinstance(item.get("buy_readiness"), dict) else {}
    checks = readiness.get("checks") if isinstance(readiness.get("checks"), dict) else {}
    price = _number(item.get("price") or item.get("latest_price"))
    snapshot = {
        "scan_id": str(scan_id or observed_at),
        "observed_at": str(observed_at),
        "asset_type": asset,
        "symbol": str(item.get("symbol") or "").upper(),
        "shadow_classification": label,
        "stage": str(item.get("stage") or item.get("signal") or "WATCH"),
        "score": _number(item.get("score")),
        "research_score": _number(item.get("early_runner_research_score")),
        "readiness_pct": _number(readiness.get("percent")),
        "failed_gate_keys": sorted(str(k) for k, passed in checks.items() if not passed)[:16],
        "price": price,
        "price_as_of": item.get("price_as_of"),
        "price_validation_status": status,
        "price_verified": verified,
        "outcome_eligible": outcome_eligible,
        "data_type": data_type,
        "data_age_seconds": age,
        "evidence": evidence,
        "features": {key: item.get(key) for key in FEATURE_FIELDS if item.get(key) is not None},
    }
    return snapshot

def append_candidate_snapshot(record, item, observed_at, scan_id=None):
    """Idempotently append one bounded observation to a candidate's history."""
    snapshot = build_candidate_snapshot(item, observed_at, scan_id or observed_at)
    if snapshot is None:
        return False
    history = record.get("signal_history")
    if not isinstance(history, list):
        history = []
    history = [row for row in history if isinstance(row, dict)
               and str(row.get("scan_id")) != snapshot["scan_id"]]
    history.append(snapshot)
    record["signal_history"] = history[-MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE:]
    return True

def build_shadow_opportunity_radar(records):
    """Summarize shadow classifications and verified scan-to-scan outcomes."""
    rows = []
    class_counts = {}
    asset_counts = {}
    total_snapshots = verified_snapshots = eligible_snapshots = 0
    for record in records or []:
        if not isinstance(record, dict):
            continue
        history = [x for x in (record.get("signal_history") or []) if isinstance(x, dict)]
        if not history:
            continue
        history.sort(key=lambda x: str(x.get("observed_at") or ""))
        total_snapshots += len(history)
        verified_snapshots += sum(bool(x.get("price_verified")) for x in history)
        eligible_snapshots += sum(bool(x.get("outcome_eligible")) for x in history)
        latest = history[-1]
        label = str(latest.get("shadow_classification") or "UNCLASSIFIED")
        asset = str(record.get("asset_type") or latest.get("asset_type") or "unknown")
        class_counts[label] = class_counts.get(label, 0) + 1
        asset_counts[asset] = asset_counts.get(asset, 0) + 1

        eligible = [x for x in history if x.get("outcome_eligible") and _number(x.get("price")) is not None and _number(x.get("price")) > 0]
        baseline = eligible[0] if eligible else None
        returns = []
        if baseline:
            base_price = _number(baseline.get("price"))
            for observation in eligible:
                price = _number(observation.get("price"))
                if price is not None and base_price:
                    returns.append((price / base_price - 1) * 100)
        recent_stages = []
        for observation in history[-8:]:
            stage = str(observation.get("stage") or "WATCH")
            if not recent_stages or recent_stages[-1] != stage:
                recent_stages.append(stage)
        scores = [_number(x.get("score")) for x in history[-3:]]
        scores = [x for x in scores if x is not None]
        rows.append({
            "asset_type": asset,
            "symbol": str(record.get("symbol") or latest.get("symbol") or ""),
            "classification": label,
            "stage": latest.get("stage") or "WATCH",
            "observations": len(history),
            "verified_observations": sum(bool(x.get("price_verified")) for x in history),
            "latest_price_status": latest.get("price_validation_status") or "unknown",
            "latest_evidence": latest.get("evidence") or [],
            "stage_progression": recent_stages,
            "score_change_last_3": round(scores[-1] - scores[0], 2) if len(scores) >= 2 else None,
            "return_from_first_eligible_pct": round(returns[-1], 2) if returns else None,
            "max_favorable_since_first_eligible_pct": round(max(returns), 2) if returns else None,
            "max_adverse_since_first_eligible_pct": round(min(returns), 2) if returns else None,
            "outcome_sample_count": len(returns),
            "notice": "Shadow research only; not an official BUY signal and not used by paper-trade entry logic.",
        })
    rows.sort(key=lambda x: (
        x["classification"] in {"BREAKOUT DEVELOPING", "PRE-BREAKOUT WATCH", "EARLY ACCUMULATION"},
        x["score_change_last_3"] if x["score_change_last_3"] is not None else -999,
        x["observations"],
    ), reverse=True)
    return {
        "mode": "shadow_only",
        "candidates_tracked": len(rows),
        "snapshots_recorded": total_snapshots,
        "verified_snapshots": verified_snapshots,
        "outcome_eligible_snapshots": eligible_snapshots,
        "unverified_snapshots": total_snapshots - verified_snapshots,
        "classification_counts": class_counts,
        "asset_counts": asset_counts,
        "max_snapshots_per_candidate": MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE,
        "candidates": rows[:50],
        "definition": "Per-scan research snapshots classified from existing indicators. Only verified prices count toward outcome measurements; stock outcomes require fresh live-session data. Labels are exploratory and never alter official BUY decisions.",
    }
