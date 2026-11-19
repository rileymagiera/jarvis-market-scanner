import numpy as np


def build_buy_intelligence(item: dict) -> dict:
    """Coverage-aware evidence model; a separate gate controls its paper-only score influence."""
    def number(key):
        try:
            value = float(item.get(key))
            return value if np.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def scaled_component(points, available_points, cap):
        if available_points <= 0:
            return None
        return round(min(cap, max(0.0, points / available_points * cap)), 1)

    tech = item.get("technical_analysis") or {}
    formation_points = formation_available = 0
    if tech.get("bollinger_squeeze") is not None:
        formation_available += 8
        if tech.get("bollinger_squeeze"): formation_points += 8
    base = number("base_tightness_pct")
    if base is not None:
        formation_available += 7
        if base <= 8: formation_points += 7
    contraction = number("atr_contraction_pct")
    if contraction is not None:
        formation_available += 5
        if contraction >= 10: formation_points += 5
    structure_value = (
        item.get("higher_highs_higher_lows")
        if item.get("asset_type") == "stock"
        else (item.get("crqs_features") or {}).get("higher_lows")
    )
    if structure_value is not None:
        formation_available += 5
        if structure_value: formation_points += 5
    breakout_keys = ("breakout_20d", "breakout_24h", "intraday_breakout")
    if any(key in item and item.get(key) is not None for key in breakout_keys):
        formation_available += 5
        if any(item.get(key) for key in breakout_keys): formation_points += 5
    formation = scaled_component(formation_points, formation_available, 25)

    momentum_points = momentum_available = 0
    features = item.get("crqs_features") or {}
    if item.get("asset_type") == "stock":
        momentum_inputs = (
            ("relative_strength_vs_spy", 8),
            ("relative_strength_vs_iwm", 5),
            ("five_day_change_pct", 4),
            ("intraday_change_pct", 3),
        )
    else:
        # Crypto relative strength is added after the full-market BTC cross-section.
        momentum_inputs = (
            ("crqs_relative_strength_vs_btc_24h_pct", 8),
            ("hourly_change_pct", 7),
        )
        four_hour = features.get("four_hour_change_pct")
        if four_hour is not None:
            try:
                four_hour = float(four_hour)
                if np.isfinite(four_hour):
                    momentum_available += 5
                    if four_hour > 0: momentum_points += 5
            except (TypeError, ValueError, OverflowError):
                pass
    for key, weight in momentum_inputs:
        value = number(key)
        if value is not None:
            momentum_available += weight
            if value > 0: momentum_points += weight
    positioning_key = "above_session_vwap" if item.get("asset_type") == "stock" else "above_24h_average"
    if item.get(positioning_key) is not None:
        momentum_available += 5
        if item.get(positioning_key):
            momentum_points += 5
    momentum = scaled_component(momentum_points, momentum_available, 25)

    activity_points = activity_available = 0
    if item.get("asset_type") == "stock":
        activity_inputs = (
            ("relative_volume", 1.5, 5), ("intraday_relative_volume", 1.5, 5),
            ("cumulative_rvol_same_time", 1.5, 5), ("volume_acceleration_3bar", 1.8, 3),
            ("premarket_rvol_same_time", 1.5, 2), ("float_rotation_pct", 20, 5),
        )
    else:
        activity_inputs = (("relative_volume", 1.5, 12),)
        consistency = features.get("volume_consistency_6h")
        if consistency is not None:
            try:
                consistency = float(consistency)
                if np.isfinite(consistency):
                    activity_available += 8
                    if consistency >= 0.70: activity_points += 8
            except (TypeError, ValueError, OverflowError):
                pass
    for key, threshold, points in activity_inputs:
        value = number(key)
        if value is not None:
            activity_available += points
            if value >= threshold: activity_points += points
    activity = scaled_component(activity_points, activity_available, 20)

    catalysts = item.get("catalyst_labels")
    dilution = item.get("dilution_flags")
    news_available = isinstance(item.get("news"), list)
    catalyst_available = catalysts is not None or dilution is not None or news_available
    if catalysts is None: catalysts = []
    if not isinstance(catalysts, list): catalysts = [str(catalysts)]
    if dilution is None: dilution = []
    if not isinstance(dilution, list): dilution = [str(dilution)]
    catalyst_score = (max(0, min(15, len(catalysts) * 5 - (10 if dilution else 0)))
                      if catalyst_available else None)

    historical = item.get("historical_setup_evidence") or {}
    observations = 0
    historical_score = None
    if isinstance(historical, dict):
        try:
            observations = max(0, int(historical.get("observations") or 0))
        except (TypeError, ValueError, OverflowError):
            observations = 0
        runner_rate = historical.get("runner_rate_pct")
        avg_return = historical.get("average_5d_return_pct")
        try:
            runner_rate = float(runner_rate) if runner_rate is not None else None
            avg_return = float(avg_return) if avg_return is not None else None
            if (observations >= 30 and runner_rate is not None and avg_return is not None
                    and np.isfinite(runner_rate) and np.isfinite(avg_return)):
                historical_score = max(0, min(15, round(
                    runner_rate * 0.10 + max(-5, min(10, avg_return)) * 0.5
                )))
        except (TypeError, ValueError, OverflowError):
            historical_score = None

    components = {
        "early_formation": formation,
        "momentum_confirmation": momentum,
        "unusual_activity": activity,
        "catalyst_quality": catalyst_score,
        "historical_edge": historical_score,
    }
    weights = {"early_formation": 25, "momentum_confirmation": 25,
               "unusual_activity": 20, "catalyst_quality": 15, "historical_edge": 15}
    available = sum(value for value in components.values() if value is not None)
    max_available = sum(weights[key] for key, value in components.items() if value is not None)
    coverage = {
        "early_formation": {"available": formation_available, "possible": 30},
        "momentum_confirmation": {"available": momentum_available, "possible": 25},
        "unusual_activity": {"available": activity_available, "possible": 25},
        "catalyst_quality": {"available": 1 if catalyst_available else 0, "possible": 1},
        "historical_edge": {"available": 1 if historical_score is not None else 0, "possible": 1},
    }
    return {
        "version": "shadow_v2",
        "status": "SHADOW ONLY — historical validation pending" if historical_score is None else "SHADOW ONLY — historical cohort available",
        "score": round(available / max_available * 100) if max_available else None,
        "weighted_points": round(available, 1),
        "maximum_available_points": max_available,
        "components": components,
        "component_coverage": coverage,
        "historical_observations": observations,
        "historical_confidence": "provisional" if historical_score is not None else "insufficient",
        "required_historical_observations": 30,
        "affects_production_buy_score": False,
        "can_execute_trades": False,
        "note": "Coverage-aware research ranking aid, not a probability. Missing inputs are excluded rather than scored as failures. Historical edge requires at least 30 comparable completed verified 5-day outcomes.",
    }



def blend_buy_score(legacy_score: int, intelligence: dict, weight: float = 0.20) -> dict:
    """Apply a bounded, coverage-gated pilot blend for paper-only BUY validation."""
    coverage = intelligence.get("component_coverage") or {}
    feature_keys = (
        "early_formation", "momentum_confirmation",
        "unusual_activity", "catalyst_quality",
    )
    available = possible = 0.0
    for key in feature_keys:
        row = coverage.get(key) or {}
        try:
            available += max(0.0, float(row.get("available") or 0))
            possible += max(0.0, float(row.get("possible") or 0))
        except (TypeError, ValueError, OverflowError):
            continue
    coverage_pct = round(available / possible * 100, 1) if possible > 0 else 0.0
    model_score = intelligence.get("score")
    try:
        model_score = float(model_score) if model_score is not None else None
        legacy = max(0.0, min(100.0, float(legacy_score)))
    except (TypeError, ValueError, OverflowError):
        legacy = 0.0
        model_score = None
    max_available = float(intelligence.get("maximum_available_points") or 0)
    applied = (
        model_score is not None
        and np.isfinite(model_score)
        and max_available >= 70
        and coverage_pct >= 60
        and 0 < weight <= 0.25
    )
    blended = round(legacy * (1 - weight) + model_score * weight) if applied else round(legacy)
    return {
        "score": max(0, min(100, int(blended))),
        "legacy_score": round(legacy),
        "intelligence_score": round(model_score, 1) if model_score is not None and np.isfinite(model_score) else None,
        "feature_coverage_pct": coverage_pct,
        "weight": weight if applied else 0.0,
        "applied": bool(applied),
        "model": "hybrid_offline_v1" if applied else "legacy_fallback",
        "note": (
            "80% legacy scanner score + 20% coverage-qualified BUY intelligence score; paper validation only."
            if applied else
            "Legacy score retained because the BUY intelligence score or feature coverage did not meet pilot requirements."
        ),
    }
