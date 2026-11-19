"""Versioned, shadow-only experiment registry stored inside the existing paper account.

Counterfactual stop variants observe the same verified scan prices as the real
position. They never create fills, positions, cash movements, or account P/L.
"""
from __future__ import annotations

from copy import deepcopy

REGISTRY_SCHEMA_VERSION = 1
MAX_OBSERVATIONS_PER_POSITION = 256
MAX_EXPERIMENT_VERSIONS = 100

DEFAULT_EXPERIMENTS = [
    {
        "experiment_id": "EARLY-ENTRY-01",
        "version": 1,
        "name": "Pre-breakout early-entry research",
        "category": "entry",
        "hypothesis": "Verified accumulation and pre-breakout evidence may identify opportunities before official BUY confirmation.",
        "status": "testing",
        "activation": "default_off_user_approval_required",
        "parameters": {"strategy": "early_entry_experimental", "score_floor": 60, "risk_off_blocked": True},
        "success_criteria": "Compare detection timing, expectancy, drawdown, false positives and missed runners against Core.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-CURRENT-01",
        "version": 1,
        "name": "Current production stop baseline",
        "category": "stop_loss",
        "hypothesis": "The current ATR/support-aware stop is the baseline for all comparisons.",
        "status": "baseline",
        "activation": "active_actual_stop",
        "parameters": {"method": "existing_levels_stop", "formula_change": False},
        "success_criteria": "Reference cohort only; compare risk, adverse excursion and outcomes.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-FIXED-025",
        "version": 1,
        "name": "Fixed 2.5% stop counterfactual",
        "category": "stop_loss",
        "hypothesis": "A fixed 2.5% stop may reduce loss size but could increase premature exits.",
        "status": "testing",
        "activation": "shadow_counterfactual_only",
        "parameters": {"method": "fixed_percent", "distance_pct": 2.5},
        "success_criteria": "Compare stop-touch frequency, observed exit return, MFE/MAE and later recovery.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-FIXED-035",
        "version": 1,
        "name": "Fixed 3.5% stop counterfactual",
        "category": "stop_loss",
        "hypothesis": "A slightly wider fixed stop may reduce noise exits while limiting downside.",
        "status": "testing",
        "activation": "shadow_counterfactual_only",
        "parameters": {"method": "fixed_percent", "distance_pct": 3.5},
        "success_criteria": "Compare stop-touch frequency, observed exit return, MFE/MAE and later recovery.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-FIXED-050",
        "version": 1,
        "name": "Fixed 5.0% stop counterfactual",
        "category": "stop_loss",
        "hypothesis": "A wider 5% stop may avoid volatility noise but could increase adverse excursion.",
        "status": "testing",
        "activation": "shadow_counterfactual_only",
        "parameters": {"method": "fixed_percent", "distance_pct": 5.0},
        "success_criteria": "Compare stop-touch frequency, observed exit return, MFE/MAE and later recovery.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-ATR-125",
        "version": 1,
        "name": "ATR 1.25x risk-unit counterfactual",
        "category": "stop_loss",
        "hypothesis": "A 1.25x ATR risk-unit stop may adapt better to volatility than fixed percentages.",
        "status": "testing",
        "activation": "shadow_counterfactual_only",
        "parameters": {"method": "atr_multiple", "multiple": 1.25},
        "success_criteria": "Compare stop-touch frequency, observed exit return, MFE/MAE and later recovery.",
        "created_at": "2026-11-19",
    },
    {
        "experiment_id": "STOP-ATR-175",
        "version": 1,
        "name": "ATR 1.75x risk-unit counterfactual",
        "category": "stop_loss",
        "hypothesis": "A 1.75x ATR risk-unit stop may better tolerate normal volatility at the cost of wider risk.",
        "status": "testing",
        "activation": "shadow_counterfactual_only",
        "parameters": {"method": "atr_multiple", "multiple": 1.75},
        "success_criteria": "Compare stop-touch frequency, observed exit return, MFE/MAE and later recovery.",
        "created_at": "2026-11-19",
    },
]


def ensure_registry(account):
    """Add registry metadata in-place without changing the account's cash or positions."""
    registry = account.get("experiment_registry")
    if not isinstance(registry, dict) or registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        registry = {"schema_version": REGISTRY_SCHEMA_VERSION, "experiments": [], "changelog": []}
        account["experiment_registry"] = registry
    known = {(x.get("experiment_id"), x.get("version")) for x in registry["experiments"] if isinstance(x, dict)}
    for definition in DEFAULT_EXPERIMENTS:
        key = (definition["experiment_id"], definition["version"])
        if key not in known:
            registry["experiments"].append(deepcopy(definition))
            registry["changelog"].append({
                "at": definition["created_at"], "experiment_id": key[0],
                "version": key[1], "event": "registered",
                "note": "Initial immutable definition; stop variants are counterfactual-only.",
            })
    registry["experiments"] = registry["experiments"][-MAX_EXPERIMENT_VERSIONS:]
    registry["changelog"] = registry["changelog"][-500:]
    return registry


def build_stop_decision_snapshot(candidate, entry_price, actual_stop, observed_at):
    """Capture the exact entry-time stop calculation and relevant available context."""
    candidate = candidate if isinstance(candidate, dict) else {}
    levels = candidate.get("levels") if isinstance(candidate.get("levels"), dict) else {}
    calculation = levels.get("calculation") if isinstance(levels.get("calculation"), dict) else {}
    quality = candidate.get("data_quality") if isinstance(candidate.get("data_quality"), dict) else {}
    feature_names = (
        "atr_14", "risk_unit", "two_percent_price_floor", "entry_buffer",
        "risk_distance", "atr_stop", "support_reference", "support_stop",
        "stop_source", "risk_per_unit",
    )
    indicators = (
        "relative_volume", "intraday_relative_volume", "change_1h_pct",
        "change_24h_pct", "volume_acceleration_3bar", "cumulative_rvol_same_time",
        "breakout_proximity_pct", "higher_lows", "accumulation_signal_score",
        "volatility_compression_percentile", "early_runner_research_score",
        "score", "score_model",
    )
    return {
        "captured_at": str(observed_at),
        "entry_price": float(entry_price),
        "actual_stop": float(actual_stop),
        "stop_calculation": {key: calculation.get(key) for key in feature_names if calculation.get(key) is not None},
        "indicators": {key: candidate.get(key) for key in indicators if candidate.get(key) is not None},
        "market_regime": candidate.get("market_regime") or "UNKNOWN",
        "price_source": candidate.get("price_source") or candidate.get("data_source"),
        "price_as_of": candidate.get("price_as_of"),
        "price_validation_passed": candidate.get("price_validation_passed") is True,
        "data_quality": {
            "data_type": quality.get("data_type"),
            "age_seconds": quality.get("age_seconds"),
            "session": quality.get("session"),
            "buy_eligible": quality.get("buy_eligible"),
        },
    }


def record_stop_observation(account, position, price, observed_at, price_as_of, scan_id=None):
    """Record one verified sampled price and evaluate counterfactual stop touches."""
    if not isinstance(position, dict) or float(price or 0) <= 0:
        return
    observations = position.setdefault("stop_observations", [])
    scan_key = str(scan_id or observed_at)
    if any(str(row.get("scan_id")) == scan_key for row in observations if isinstance(row, dict)):
        return
    row = {
        "scan_id": scan_key, "observed_at": str(observed_at),
        "price_as_of": price_as_of, "price": float(price),
    }
    observations.append(row)
    del observations[:-MAX_OBSERVATIONS_PER_POSITION]
    entry = float(position.get("entry_price") or 0)
    if entry <= 0:
        return
    calc = (position.get("stop_decision_snapshot") or {}).get("stop_calculation") or {}
    risk_unit = calc.get("risk_unit")
    actual_stop = float(position.get("stop_loss") or 0)
    for definition in ensure_registry(account)["experiments"]:
        if definition.get("category") != "stop_loss" or definition.get("activation") != "shadow_counterfactual_only":
            continue
        experiment_id = definition["experiment_id"]
        result = position.setdefault("stop_counterfactuals", {}).setdefault(experiment_id, {
            "experiment_id": experiment_id, "version": definition["version"],
            "stop_price": None, "first_touch": None, "observations": 0,
            "status": "tracking",
        })
        params = definition.get("parameters") or {}
        if params.get("method") == "fixed_percent":
            stop_price = entry * (1 - float(params["distance_pct"]) / 100)
        elif params.get("method") == "atr_multiple" and risk_unit:
            stop_price = entry - float(params["multiple"]) * float(risk_unit)
        else:
            continue
        result["stop_price"] = round(stop_price, 10)
        result["observations"] += 1
        if result["first_touch"] is None and float(price) <= stop_price:
            result["first_touch"] = {
                "observed_at": str(observed_at), "price_as_of": price_as_of,
                "observed_price": float(price),
                "return_from_entry_pct": round((float(price) / entry - 1) * 100, 4),
                "note": "Counterfactual threshold touched by a verified scan sample; not a simulated fill.",
            }
            result["status"] = "touched_on_sample"
    position["actual_stop_reference"] = actual_stop


def summarize_experiment_registry(account):
    registry = ensure_registry(account)
    positions = list(account.get("positions") or []) + list(account.get("closed_trades") or [])
    rows = []
    for definition in registry["experiments"]:
        experiment_id = definition.get("experiment_id")
        linked = [p for p in positions if p.get("experiment_registry_version") == "{}-v{}".format(experiment_id, definition.get("version")) or p.get("stop_experiment_version") == "{}-v{}".format(experiment_id, definition.get("version"))]
        touches = [p.get("stop_counterfactuals", {}).get(experiment_id, {})
                   for p in positions if experiment_id in (p.get("stop_counterfactuals") or {})]
        touched = sum(1 for x in touches if x.get("first_touch"))
        rows.append({
            **deepcopy(definition),
            "linked_positions": len(linked),
            "counterfactual_positions_observed": len(touches),
            "sample_touches": touched,
            "sample_status": "insufficient sample" if len(touches) < 30 else "descriptive sample",
        })
    return {"schema_version": registry["schema_version"], "experiments": rows,
            "changelog": deepcopy(registry.get("changelog") or [])[-100:],
            "notice": "Counterfactual stop touches are based on verified scan samples, not intrabar paths or fills. They never affect cash, positions, exits, or realized account P/L."}
