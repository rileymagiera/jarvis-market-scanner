"""Paper-only research profiles adapted from publicly documented strategy families.

No BUY eligibility, risk limits, capital, stop/target levels, or live execution
permissions are changed here. Profiles are evidence labels, not win probabilities.
"""
import math


PAPER_EDGE_MODEL_VERSION = "github-confluence-v1"
PAPER_EDGE_SOURCE = (
    "Research ideas: erhungn/volatility-regime-momentum and "
    "crollila/strategy-survival-funnel. Adapted to existing Jarvis features; "
    "their reported backtests are not a measured Jarvis win rate."
)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _any_known(*checks):
    known = [value for value in checks if isinstance(value, bool)]
    return any(known) if known else None


def _above(value, cutoff):
    number = _number(value)
    return number > cutoff if number is not None else None


def paper_edge_profile(candidate):
    """Measure independent, already-calculated evidence without fetching more data.

    A soft ATR rule adapts the source's 0.95 volatility ratio to Jarvis's
    existing ATR contraction vs prior-window mean: contraction <= +5%.
    This is an *analogue*, not the source's exact strategy or exit model.
    Missing features stay unknown and never count as positive evidence.
    """
    item = candidate if isinstance(candidate, dict) else {}
    asset = str(item.get("asset_type") or "").lower()
    tech = item.get("technical_analysis") if isinstance(item.get("technical_analysis"), dict) else {}
    atr_contraction = _number(item.get("atr_contraction_pct"))
    soft_atr_expansion = atr_contraction <= 5 if atr_contraction is not None else None

    if asset == "stock":
        momentum = _above(item.get("five_day_change_pct"), 0)
        relative = _any_known(
            _above(item.get("relative_strength_vs_spy"), 0),
            _above(item.get("relative_strength_vs_iwm"), 0),
        )
    elif asset == "crypto":
        momentum = _any_known(
            _above(item.get("hourly_change_pct"), 0),
            _above(item.get("change_24h_pct"), 0),
        )
        relative = _above(item.get("crqs_relative_strength_vs_btc_24h_pct"), 0)
    else:
        momentum, relative = None, None

    factors = {
        "trend_alignment": _any_known(
            tech.get("ema_9_above_ema_20"),
            item.get("multi_timeframe_alignment"),
        ),
        "soft_volatility_expansion": _any_known(
            tech.get("bollinger_squeeze_expansion"), soft_atr_expansion,
        ),
        "positive_momentum": momentum,
        "relative_strength": relative,
        "volume_participation": _any_known(
            _above(item.get("relative_volume"), 1.3),
            _above(item.get("intraday_relative_volume"), 1.5),
            _above(item.get("volume_acceleration_3bar"), 1.0),
        ),
        "accumulation": _any_known(
            tech.get("obv_trend_up_10d"),
            _above(item.get("accumulation_signal_score"), 49),
        ),
    }
    required = ("trend_alignment", "soft_volatility_expansion",
                "positive_momentum", "volume_participation")
    qualified = all(factors[name] is True for name in required)
    if qualified:
        cohort = "trend_volatility_confluence"
    elif factors["trend_alignment"] is True and factors["relative_strength"] is True and factors["volume_participation"] is True:
        cohort = "relative_strength_confluence"
    elif factors["accumulation"] is True and factors["volume_participation"] is True:
        cohort = "accumulation_confluence"
    else:
        cohort = "unconfirmed_or_incomplete"

    return {
        "version": PAPER_EDGE_MODEL_VERSION,
        "cohort": cohort,
        "qualified_trend_volatility": qualified,
        "confirmed_factors": sum(value is True for value in factors.values()),
        "available_factors": sum(value is not None for value in factors.values()),
        "factor_count": len(factors),
        "factors": factors,
        "research_note": "Adapted paper-only classification; no historical win rate implied",
    }
