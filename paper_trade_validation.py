"""Read-only paper-trade outcome checks inspired by Jesse and strategy-survival research.

Does not place orders, select trades, change any strategy gate, or alter account state.
Results are descriptive closed-trade statistics, NOT predictive success probabilities.
"""
from collections import defaultdict
from math import isfinite, sqrt

WILSON_Z = 1.959963984540054
MIN_DIRECTIONAL_SAMPLE = 30


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
        return num if isfinite(num) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _key(value, default="unknown"):
    return str(value).strip() if value is not None and str(value).strip() else default


def _wilson_interval(wins, total):
    """95% Wilson score interval; descriptive, not a probability forecast."""
    if not total:
        return None
    p = wins / total
    z2 = WILSON_Z ** 2
    divisor = 1 + z2 / total
    center = (p + z2 / (2 * total)) / divisor
    margin = WILSON_Z * sqrt((p * (1 - p) + z2 / (4 * total)) / total) / divisor
    return [round(max(0, center - margin) * 100, 2),
            round(min(1, center + margin) * 100, 2)]


def _metrics(rows):
    """One row per completed position, including all allocated entry/exit costs."""
    pnl = [row["_validated_pnl"] for row in sorted(rows, key=lambda r: _key(r.get("closed_at"), "9999"))]
    n = len(pnl)
    wins = sum(x > 0 for x in pnl)
    losses = sum(x < 0 for x in pnl)
    gross_profit = sum(x for x in pnl if x > 0)
    gross_loss = -sum(x for x in pnl if x < 0)

    # This is closed-trade realized P/L drawdown, NOT an intratrade equity drawdown.
    running = peak = max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for amount in pnl:
        running += amount
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        losing_streak = losing_streak + 1 if amount < 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)

    return {
        "closed_trades": n,
        "wins": wins,
        "losses": losses,
        "breakeven": n - wins - losses,
        "win_rate_pct": round(100 * wins / n, 2) if n else None,
        "win_rate_wilson_95_pct": _wilson_interval(wins, n),
        "net_realized_pnl": round(sum(pnl), 4),
        "expectancy_net_per_trade": round(sum(pnl) / n, 4) if n else None,
        "average_win": round(gross_profit / wins, 4) if wins else None,
        "average_loss_abs": round(gross_loss / losses, 4) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "closed_pnl_max_drawdown": round(max_drawdown, 4),
        "longest_closed_losing_streak": max_losing_streak,
        "sample_status": "directional_review_only" if n >= MIN_DIRECTIONAL_SAMPLE else "insufficient_sample",
    }


def build_paper_trade_validation(account):
    """Read-only grouped outcome report using only completed, recorded paper trades.

    Cohorts use entry-time frozen labels; unstamped historical trades are excluded
    from profile cohorts rather than being retroactively classified.
    """
    raw = account.get("closed_trades") or [] if isinstance(account, dict) else []
    valid = []
    for trade in raw:
        if not isinstance(trade, dict):
            continue
        pnl = _finite(trade.get("realized_pnl"))
        if pnl is None:
            continue
        valid.append({**trade, "_validated_pnl": pnl})

    by_strategy = defaultdict(list)
    by_regime = defaultdict(list)
    by_asset = defaultdict(list)
    by_cohort = defaultdict(list)
    for trade in valid:
        strategy = _key(trade.get("strategy"), "official_buy")
        regime = _key(trade.get("market_regime_at_entry"), "UNKNOWN").upper()
        asset = _key(trade.get("asset_type")).lower()
        by_strategy[strategy].append(trade)
        by_regime[(strategy, regime)].append(trade)
        by_asset[(strategy, asset)].append(trade)
        profile = trade.get("paper_edge_profile")
        if isinstance(profile, dict) and profile.get("version") and profile.get("cohort"):
            by_cohort[(profile["version"], profile["cohort"])].append(trade)

    def breakdown(groups, names):
        result = []
        for key, trades in sorted(groups.items(), key=lambda kv: str(kv[0])):
            fields = dict(zip(names, key if isinstance(key, tuple) else (key,)))
            result.append({**fields, **_metrics(trades)})
        return result

    return {
        "model": "paper-outcome-validation-v1",
        "reported_closed_trades": len(raw),
        "valid_closed_trades": len(valid),
        "excluded_invalid_pnl": len(raw) - len(valid),
        "overall": _metrics(valid),
        "by_strategy": breakdown(by_strategy, ["strategy"]),
        "by_strategy_regime": breakdown(by_regime, ["strategy", "entry_regime"]),
        "by_strategy_asset": breakdown(by_asset, ["strategy", "asset_type"]),
        "by_frozen_edge_cohort": breakdown(by_cohort, ["profile_version", "cohort"]),
        "interpretation": (
            "A 95% Wilson interval describes uncertainty in the observed binary win rate; "
            "it is not a forecast. A minimum of 30 closed trades is a reporting label, "
            "not proof of an edge. Real performance requires chronological out-of-sample "
            "testing, net-of-cost fills, and separate stock/crypto and regime analysis. "
            "Closed-P/L drawdown excludes open-position and intrabar movement. "
            "No BUY or paper-entry logic, capital, thresholds, fees, or scan schedule changed."
        ),
    }
