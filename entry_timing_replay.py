"""Fee-aware research replay of trade levels frozen in a timing-lab snapshot."""
from collections import Counter
from math import isfinite
from statistics import mean

from early_opportunity_radar import REPLAY_LEVEL_FIELDS
from paper_trading import calculate_trade_fee

NOTIONAL = 100.0
ADVERSE_EXIT_BPS = 50


def saved_plan(snapshot, entry_price):
    """Validate the original plan against the next eligible quote."""
    levels = snapshot.get("replay_levels")
    if not isinstance(levels, dict):
        return "missing_plan", None
    values = [levels.get(key) for key in REPLAY_LEVEL_FIELDS]
    if any(type(value) not in (int, float) or not isfinite(value) or value <= 0
           for value in values):
        return "invalid_or_incomplete_plan", None
    low, high, stop, first_target, second_target = values
    if high < low or not stop < first_target <= second_target:
        return "invalid_or_incomplete_plan", None
    if not (low <= entry_price <= high and stop < entry_price < first_target):
        return "quote_outside_plan", None
    return "eligible_plan", (stop, first_target, second_target)


def replay_outcome(asset, entry_price, plan, observed_prices):
    """Apply the paper exit order to sampled prices through one research horizon."""
    stop, first_target, second_target = plan
    exits = []
    remaining = 1.0
    for price in observed_prices:
        if price <= stop:
            reason = "STOP"
        elif price >= second_target:
            reason = "TARGET_2"
        elif remaining == 1 and price >= first_target:
            exits.append((0.5, price))
            remaining = 0.5
            continue
        else:
            continue
        exits.append((remaining, price))
        break
    else:
        reason = "HORIZON"
        exits.append((remaining, observed_prices[-1]))

    quantity = NOTIONAL / entry_price
    entry_fee = calculate_trade_fee(asset, "buy", NOTIONAL, quantity)["total"]
    results = {"exit_reason": reason}
    for label, haircut in (("observed_quote", 0), ("adverse_exit", ADVERSE_EXIT_BPS / 10_000)):
        net_proceeds = 0.0
        for fraction, price in exits:
            sell_quantity = quantity * fraction
            gross = sell_quantity * price * (1 - haircut)
            net_proceeds += gross - calculate_trade_fee(asset, "sell", gross, sell_quantity)["total"]
        results[label] = net_proceeds - NOTIONAL - entry_fee
    return results


def summarize_replay(outcomes):
    count = len(outcomes)
    gross_returns = [outcome["gross_horizon_return_pct"] for outcome in outcomes]
    summary = {
        "sample_count": count,
        "exit_reasons": dict(Counter(outcome["exit_reason"] for outcome in outcomes)),
        "mean_gross_horizon_return_pct": round(mean(gross_returns), 4) if count else None,
        "positive_gross_horizon_pct": (
            round(100 * sum(value > 0 for value in gross_returns) / count, 2) if count else None
        ),
    }
    for scenario in ("observed_quote", "adverse_exit"):
        profits = [outcome[scenario] for outcome in outcomes]
        wins = sum(value > 1e-8 for value in profits)
        summary[scenario] = {
            "net_wins": wins,
            "net_win_rate_pct": round(100 * wins / count, 2) if count else None,
            "mean_net_return_pct": round(mean(profits), 4) if count else None,
        }
    return summary
