"""Read-only combination audit using Jarvis's EXISTING per-scan BUY gate history.

No market-data requests, new price storage, trade simulation, strategy changes,
or broker operations. Results are sampled post-observation excursions, not fills.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import isfinite
from statistics import median

# Each profile relaxes only named research conditions in a *shadow comparison*.
# Official BUY gates and paper account remain completely unchanged.
PROFILES = (
    ("stock_score_only", "stock", ("score_82",)),
    ("stock_volume_only", "stock", ("volume_confirmed",)),
    ("stock_breakout_only", "stock", ("breakout_confirmed",)),
    ("stock_score_volume_breakout", "stock", ("score_82", "volume_confirmed", "breakout_confirmed")),
    ("crypto_score_only", "crypto", ("score_82",)),
    ("crypto_volume_only", "crypto", ("volume_1_8x",)),
    ("crypto_breakout_only", "crypto", ("24h_breakout",)),
    ("crypto_score_volume_breakout", "crypto", ("score_82", "volume_1_8x", "24h_breakout")),
    # Separately labelled regime experiment. NEVER a production or paper BUY.
    ("crypto_score_breakout_risk_off_shadow", "crypto",
     ("score_82", "24h_breakout", "market_not_risk_off")),
)
MIN_DIRECTIONAL_CANDIDATES = 30
SAFETY_GATES = frozenset((
    "price_verified", "live_data_quality", "no_risk_flags", "not_chase_risk",
    "not_extended",
))


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _usable_event(event):
    """Don't let missing or unverified signal-time prices masquerade as entries."""
    if not isinstance(event, dict) or event.get("price_verified") is not True:
        return False
    price = _finite(event.get("price"))
    if price is None or price <= 0 or _timestamp(event.get("at")) is None:
        return False
    failed = event.get("failed_gate_keys")
    if not isinstance(failed, list) or not all(isinstance(k, str) for k in failed):
        return False
    return not SAFETY_GATES.intersection(failed)


def _profile_record(records, asset, relax):
    """Select first qualifying signal per distinct symbol, never cherry-pick a later win.

    For nonqualifiers, retain the fewest *other* failed gates observed for the
    same symbol to explain why relaxing this profile still would not suffice.
    """
    selected = {}
    blockers = {}
    observed = set()
    potential = set()
    invalid_events = 0
    for record in records:
        if not isinstance(record, dict) or record.get("asset_type") != asset:
            continue
        symbol = str(record.get("symbol") or "").upper()
        if not symbol:
            continue
        for event in record.get("buy_gate_history") or []:
            if not _usable_event(event):
                invalid_events += 1
                continue
            failed = frozenset(event["failed_gate_keys"])
            if not failed.intersection(relax):
                continue  # baseline passing signals are not new opportunities
            observed.add(symbol)
            remaining = failed - relax
            at = _timestamp(event["at"])
            if not remaining:
                candidate = selected.get(symbol)
                if candidate is None or at < candidate[0]:
                    selected[symbol] = (at, event)
            else:
                item = blockers.get(symbol)
                if item is None or (len(remaining), at) < (len(item[0]), item[1]):
                    blockers[symbol] = (remaining, at)
            potential.add(symbol)
    # Symbols with a qualifying event are not classified as blocked by another gate.
    blocked = {symbol: value for symbol, value in blockers.items() if symbol not in selected}
    counter = Counter(key for remaining, _ in blocked.values() for key in remaining)
    return selected, len(observed), blocked, counter, invalid_events


def build_gate_combination_shadow(records):
    """Compare existing failed-gate combinations; never infer executable profits."""
    data = [row for row in (records or []) if isinstance(row, dict)]
    profiles = []
    for name, asset, keys in PROFILES:
        relaxed = frozenset(keys)
        selected, exposed, blocked, remaining, invalid = _profile_record(data, asset, relaxed)
        first_events = [event for _, event in selected.values()]
        measured = [
            (favorable, adverse)
            for event in first_events
            if (int(event.get("follow_up_observations") or 0) > 0)
            for favorable, adverse in [(
                _finite(event.get("max_favorable_since_pct")),
                _finite(event.get("max_adverse_since_pct")),
            )]
            if favorable is not None and adverse is not None
        ]
        favorable = [pair[0] for pair in measured]
        adverse = [pair[1] for pair in measured]
        qualified_count = len(selected)
        profiles.append({
            "name": name,
            "asset_type": asset,
            "relaxed_gates": list(keys),
            "regime_gate_relaxed": "market_not_risk_off" in relaxed,
            "candidate_symbols_with_at_least_one_relaxed_gate_failed": exposed,
            "newly_qualifying_unique_candidates": qualified_count,
            "still_blocked_by_other_gates": len(blocked),
            "top_remaining_blockers": [
                {"gate": key, "unique_candidates": count}
                for key, count in remaining.most_common(5)
            ],
            "newly_qualifying_with_measured_followup": len(measured),
            "sampled_later_reached_5pct": sum(x >= 5 for x in favorable),
            "sampled_later_reached_10pct": sum(x >= 10 for x in favorable),
            "sampled_later_reached_20pct": sum(x >= 20 for x in favorable),
            "sampled_adverse_at_least_3pct": sum(x <= -3 for x in adverse),
            "median_max_favorable_pct": round(median(favorable), 2) if favorable else None,
            "median_max_adverse_pct": round(median(adverse), 2) if adverse else None,
            "sample_status": ("directional_only_not_executable" if len(measured) >= MIN_DIRECTIONAL_CANDIDATES
                              else "insufficient_unique_followup"),
            "excluded_unverified_or_invalid_gate_events": invalid,
        })
    return {
        "version": "gate-combination-shadow-v1",
        "mode": "read_only_existing_history",
        "production_effect": "none",
        "minimum_unique_followups_for_directional_label": MIN_DIRECTIONAL_CANDIDATES,
        "profiles": profiles,
        "limitations": [
            "Newly qualifying means no remaining recorded BUY readiness failure, not proof a trade would fill.",
            "Signal-time verified prices are required; unknown or unverified records are excluded, not counted as losses.",
            "Only retained snapshots are available; no missing-data imputation or expanded universe.",
            "One earliest qualifying recorded event per asset/symbol/profile prevents repeated-scan inflation.",
            "Future favorable/adverse percentages are sampled observations; intrabar stops, order fills and fees are unknown.",
            "Relaxed regime scenarios are research labels only and cannot bypass official or experimental account controls.",
            "Risk checks and entry data-quality checks are NEVER relaxed in these profiles.",
            "These combinations reuse existing gate booleans; numeric score/volume threshold backtests need frozen signal-time inputs.",
            "This analysis does not add API calls, alter BUY/risk/strategy rules, or place paper or real trades.",
        ],
    }
