"""Read-only sampled lead-time audit of existing per-scan Jarvis signals.

This module never alters scanning, BUY rules, historical state, paper capital,
prices, or trading permissions. A crossing is observed at a scan, not intrabar.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import isfinite
from statistics import median

THRESHOLDS_PCT = (5, 10, 20)
MIN_REVIEW_CANDIDATES = 8
MAX_EXAMPLES = 25
MAX_COHORT_ROWS = 120


def _time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _positive(value):
    try:
        num = float(value)
        return num if isfinite(num) and num > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _labels(snapshot):
    labels = []
    classification = snapshot.get("shadow_classification")
    if isinstance(classification, str) and classification.strip():
        labels.append(("classification", classification.strip()[:100]))
    evidence = snapshot.get("evidence")
    if isinstance(evidence, list):
        for item in evidence[:12]:
            if isinstance(item, str) and item.strip():
                labels.append(("indicator", item.strip()[:100]))
    return list(dict.fromkeys(labels))


def _valid_observations(record):
    asset = str(record.get("asset_type") or "").lower()
    if asset not in ("stock", "crypto"):
        return [], 0
    observations = []
    rejected = 0
    for snapshot in record.get("signal_history") or []:
        if not isinstance(snapshot, dict):
            rejected += 1
            continue
        observed_at = _time(snapshot.get("observed_at"))
        quote_at = _time(snapshot.get("price_as_of"))
        price = _positive(snapshot.get("price"))
        # The existing outcome_eligible flag enforces stock session quality.
        if not (snapshot.get("outcome_eligible") is True
                and snapshot.get("price_verified") is True
                and observed_at and quote_at and price):
            rejected += 1
            continue
        age_seconds = (observed_at - quote_at).total_seconds()
        if not 0 <= age_seconds <= 900:
            rejected += 1
            continue
        observations.append((observed_at, quote_at, price, snapshot))
    observations.sort(key=lambda row: (row[0], row[1]))
    seen_scans = set()
    filtered = []
    for row in observations:
        scan_id = str(row[3].get("scan_id") or row[0].isoformat())
        if scan_id in seen_scans:
            rejected += 1
            continue
        seen_scans.add(scan_id)
        filtered.append(row)
    return filtered, rejected


def build_signal_lead_time_audit(records):
    """Compare first eligible signal observations with later sampled price gains.

    Baseline: first retained verified observation per candidate. All signals,
    gains, and scan times are constrained to that retained observation window.
    A candidate contributes only once per signal/threshold cohort.
    """
    summary = defaultdict(lambda: {
        "signals_observed": 0, "signals_with_followup": 0,
        "later_crossings": 0, "crossed_before_or_at_signal": 0,
        "lead_minutes": [],
    })
    followed_candidates = eligible_candidates = eligible_snapshots = excluded_snapshots = 0
    examples = []
    example_keys = set()
    for record in records or []:
        if not isinstance(record, dict):
            continue
        asset = str(record.get("asset_type") or "").lower()
        observations, rejected = _valid_observations(record)
        excluded_snapshots += rejected
        if not observations:
            continue
        eligible_candidates += 1
        eligible_snapshots += len(observations)
        if len(observations) < 2:
            continue
        followed_candidates += 1
        baseline = observations[0][2]
        first_signals = {}
        for scan_at, _, _, snapshot in observations:
            for label in _labels(snapshot):
                first_signals.setdefault(label, scan_at)
        threshold_hits = {}
        for threshold in THRESHOLDS_PCT:
            for scan_at, _, price, _ in observations:
                if (price / baseline - 1) * 100 >= threshold:
                    threshold_hits[threshold] = scan_at
                    break
        for (kind, label), signal_at in first_signals.items():
            has_followup = observations[-1][0] > signal_at
            for threshold in THRESHOLDS_PCT:
                stats = summary[(asset, kind, label, threshold)]
                stats["signals_observed"] += 1
                if not has_followup:
                    continue
                stats["signals_with_followup"] += 1
                hit_at = threshold_hits.get(threshold)
                if not hit_at:
                    continue
                if hit_at <= signal_at:
                    stats["crossed_before_or_at_signal"] += 1
                    continue
                lead = round((hit_at - signal_at).total_seconds() / 60, 1)
                stats["later_crossings"] += 1
                stats["lead_minutes"].append(lead)
                example_key = (asset, str(record.get("symbol") or ""), threshold)
                if example_key not in example_keys and len(examples) < MAX_EXAMPLES:
                    example_keys.add(example_key)
                    examples.append({
                        "asset_type": asset,
                        "symbol": str(record.get("symbol") or "")[:30],
                        "indicator_type": kind,
                        "signal": label,
                        "threshold_pct": threshold,
                        "first_signal_at": signal_at.isoformat(),
                        "first_observed_crossing_at": hit_at.isoformat(),
                        "lead_minutes": lead,
                    })
    rows = []
    for (asset, kind, label, threshold), stats in summary.items():
        leads = stats["lead_minutes"]
        rows.append({
            "asset_type": asset,
            "indicator_type": kind,
            "signal": label,
            "threshold_pct": threshold,
            "signals_observed": stats["signals_observed"],
            "signals_with_followup": stats["signals_with_followup"],
            "later_observed_crossings": stats["later_crossings"],
            "already_crossed_at_first_signal": stats["crossed_before_or_at_signal"],
            "median_lead_minutes": round(median(leads), 1) if leads else None,
            "sample_status": (
                "directional review only" if stats["signals_with_followup"] >= MIN_REVIEW_CANDIDATES
                else "insufficient sample · do not rank"
            ),
        })
    rows.sort(key=lambda x: (
        -x["signals_with_followup"], x["asset_type"],
        x["indicator_type"], x["signal"], x["threshold_pct"],
    ))
    return {
        "mode": "read_only_research",
        "thresholds_pct": list(THRESHOLDS_PCT),
        "eligible_candidates": eligible_candidates,
        "candidates_with_followup": followed_candidates,
        "eligible_snapshots": eligible_snapshots,
        "excluded_snapshots": excluded_snapshots,
        "cohort_rows_total": len(rows),
        "cohorts_truncated": len(rows) > MAX_COHORT_ROWS,
        "cohorts": rows[:MAX_COHORT_ROWS],
        "examples": examples,
        "min_candidates_for_directional_review": MIN_REVIEW_CANDIDATES,
        "definitions": [
            "Lead time is elapsed minutes between first eligible signal scan and first later eligible observed threshold scan, not the actual intrabar move.",
            "Returns use the first retained verified snapshot as baseline, not a hypothetical trade entry.",
            "Only 32 recent snapshots per candidate are retained; earlier signals or later outcomes may be missing.",
            "Both timestamps must be timezone-aware, the price independently verified, and the quote no more than 900 seconds old.",
            "At-or-after-crossing signals never count as early warnings; missing follow-up is not a failed signal.",
            "Overlapping signals are correlated and do not establish win rates, causality, or executable P/L.",
        ],
    }
