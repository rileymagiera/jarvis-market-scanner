"""Read-only entry-timing research from Jarvis's existing verified scan snapshots.

This lab never emits BUY signals, trades, alert recommendations, or strategy
threshold changes. Hypothetical entry is the *next eligible observation* after
a research label, not the signal price. Subsequent outcomes are sampled gross
returns, not backtested fills or forward win-probability predictions.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from signal_lead_time_audit import _valid_observations

MODEL_VERSION = "entry-timing-next-scan-v1"
PHASES = (
    "EARLY WATCH", "EARLY ACCUMULATION",
    "PRE-BREAKOUT WATCH", "BREAKOUT DEVELOPING",
)
HORIZONS_MIN = (60, 240)
MAX_NEXT_SCAN_MIN = 30
MAX_OUTCOME_DELAY_MIN = 30
MIN_DIRECTIONAL_SAMPLE = 30
MAX_EXAMPLES = 24


def _mean(values):
    return round(sum(values) / len(values), 3) if values else None


def _median(values):
    return round(median(values), 3) if values else None


def _followup(series, entry_index, minutes):
    """First verified observation at/after the horizon inside a bounded window."""
    entry_at, entry_quote, entry_price, _ = series[entry_index]
    for i in range(entry_index + 1, len(series)):
        observed_at, quoted_at, price, _ = series[i]
        age_min = (observed_at - entry_at).total_seconds() / 60
        if age_min < minutes:
            continue
        if age_min > minutes + MAX_OUTCOME_DELAY_MIN:
            break
        if quoted_at <= entry_quote:  # no repeated unchanged quote as follow-up
            continue
        path = [point[2] for point in series[entry_index:i + 1]
                if entry_quote <= point[1] <= quoted_at]
        if not path:
            return None
        return {
            "return_pct": (price / entry_price - 1) * 100,
            "sampled_favorable_pct": (max(path) / entry_price - 1) * 100,
            "sampled_adverse_pct": (min(path) / entry_price - 1) * 100,
            "observed_minutes": round(age_min, 1),
            "end_at": observed_at.isoformat(),
        }
    return None


def build_entry_timing_lab(records):
    """Compare early-research phase timing; never claim those phases are official BUYs.

    A phase contributes only its first retained verified observation per
    candidate. Next-scan entry requires a strictly newer independent quote
    within 30 minutes. Missing follow-up is recorded, never counted as a loss.
    For directional research only: no executable trades, no intrabar prices.
    """
    cohorts = defaultdict(list)
    eligible_candidates = evaluated = excluded = no_next_scan = 0
    examples = []
    by_asset = {"stock": 0, "crypto": 0}

    for record in records or []:
        if not isinstance(record, dict):
            continue
        asset = str(record.get("asset_type") or "").lower()
        if asset not in by_asset:
            continue
        series, rejected = _valid_observations(record)
        excluded += rejected
        if not series:
            continue
        eligible_candidates += 1
        symbol = str(record.get("symbol") or "").upper()[:24]
        seen_phases = set()
        for idx, (signal_at, signal_quote, signal_price, snapshot) in enumerate(series):
            phase = str(snapshot.get("shadow_classification") or "").upper()
            if phase not in PHASES or phase in seen_phases:
                continue
            seen_phases.add(phase)
            evaluated += 1
            if idx + 1 >= len(series):
                no_next_scan += 1
                continue
            entry_at, entry_quote, entry_price, _ = series[idx + 1]
            minutes_to_entry = (entry_at - signal_at).total_seconds() / 60
            if not (0 < minutes_to_entry <= MAX_NEXT_SCAN_MIN and entry_quote > signal_quote):
                no_next_scan += 1
                continue
            by_asset[asset] += 1
            for horizon in HORIZONS_MIN:
                result = _followup(series, idx + 1, horizon)
                if result is None:
                    continue
                cohort_key = (asset, phase, horizon)
                cohorts[cohort_key].append({
                    **result,
                    "signal_at": signal_at.isoformat(),
                    "entry_at": entry_at.isoformat(),
                    "signal_price": signal_price,
                    "entry_price": entry_price,
                    "minutes_to_entry": round(minutes_to_entry, 1),
                    "symbol": symbol,
                })
                if len(examples) < MAX_EXAMPLES:
                    examples.append({
                        "asset_type": asset,
                        "symbol": symbol,
                        "phase": phase,
                        "horizon_minutes": horizon,
                        "signal_at": signal_at.isoformat(),
                        "next_scan_at": entry_at.isoformat(),
                        "next_scan_price": round(entry_price, 8),
                        "subsequent_gross_return_pct": round(result["return_pct"], 3),
                    })

    rows = []
    for (asset, phase, horizon), outcomes in sorted(cohorts.items()):
        returns = [row["return_pct"] for row in outcomes]
        n = len(returns)
        rows.append({
            "asset_type": asset,
            "research_phase": phase,
            "horizon_minutes": horizon,
            "sampled_candidates": n,
            "positive_gross_followups": sum(x > 0 for x in returns),
            "positive_gross_followup_pct": round(100 * sum(x > 0 for x in returns) / n, 2),
            "mean_gross_return_pct": _mean(returns),
            "median_gross_return_pct": _median(returns),
            "median_sampled_favorable_pct": _median([row["sampled_favorable_pct"] for row in outcomes]),
            "median_sampled_adverse_pct": _median([row["sampled_adverse_pct"] for row in outcomes]),
            "median_next_scan_delay_min": _median([row["minutes_to_entry"] for row in outcomes]),
            "review_status": ("directional_only" if n >= MIN_DIRECTIONAL_SAMPLE
                              else "insufficient_sample_do_not_rank"),
        })
    rows.sort(key=lambda r: (r["asset_type"], r["horizon_minutes"],
                             -r["sampled_candidates"], r["research_phase"]))
    return {
        "version": MODEL_VERSION,
        "mode": "read_only_shadow_analysis",
        "hypothetical_entry": "first subsequent verified scan within 30 minutes, newer quote required",
        "horizons_minutes": list(HORIZONS_MIN),
        "eligible_candidates": eligible_candidates,
        "eligible_phase_events": evaluated,
        "events_without_next_fresh_scan": no_next_scan,
        "excluded_invalid_snapshots": excluded,
        "next_scan_samples_by_asset": by_asset,
        "cohorts": rows,
        "examples": examples,
        "limitations": [
            "Research phases are not official BUYs. Gross sampled returns are not actual filled trades, net profits, or a win-rate prediction.",
            "An actual order can fill worse than the next snapshot, or not fill. Fees, spread, slippage, stops, and targets are not modeled here.",
            "A 60/240-minute horizon requires a later fresh observation within 30 minutes; missing outcomes are excluded, not called losses.",
            "The first retained phase event per candidate is measured once. Only the most recent 32 scans may be retained.",
            "Observed sampled highs and lows are not intrabar prices and cannot prove favorable order of stops versus targets.",
            "Phase cohorts from the same candidate overlap. No cross-cohort causal inference or strategy promotion is justified.",
            "No changes to official BUY, Risk-Off, account state, paper capital, alerts, fees, or scan timing.",
        ],
    }
