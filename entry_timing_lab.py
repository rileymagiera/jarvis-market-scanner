"""Read-only entry-timing research from Jarvis's existing verified scan snapshots.

This lab never emits BUY signals, trades, alert recommendations, or strategy
threshold changes. Hypothetical entry is the *next eligible observation* after
a research label, not the signal price. Subsequent outcomes are sampled gross
returns, not backtested fills or forward win-probability predictions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from signal_lead_time_audit import _valid_observations
from early_opportunity_radar import _eligible_snapshot
from entry_timing_replay import ADVERSE_EXIT_BPS, replay_outcome, saved_plan, summarize_replay

MODEL_VERSION = "entry-timing-next-scan-v2"
PHASES = (
    "EARLY WATCH", "EARLY ACCUMULATION",
    "PRE-BREAKOUT WATCH", "BREAKOUT DEVELOPING",
)
HORIZONS_MIN = (60, 240)
MAX_NEXT_SCAN_MIN = 30
MAX_OUTCOME_DELAY_MIN = 30
MIN_DIRECTIONAL_SAMPLE = 30
MAX_EXAMPLES = 24
# Research-only thresholds. Not paper-trade stop/target or BUY parameters.
SAMPLED_STOP_PCT = -3.0
SAMPLED_RALLY_PCT = 8.0
MAX_PATH_EXAMPLES = 16

def _sampled_threshold_order(path):
    """Order threshold crossings on a path of strictly newer verified quotes."""
    entry_price = path[0][2]
    first_stop_at = first_rally_at = None
    for _, at, price, _ in path[1:]:
        move_pct = (price / entry_price - 1) * 100
        if first_stop_at is None and move_pct <= SAMPLED_STOP_PCT:
            first_stop_at = at
        if first_rally_at is None and move_pct >= SAMPLED_RALLY_PCT:
            first_rally_at = at
    if first_stop_at and first_rally_at:
        classification = ("sampled_stop_before_rally" if first_stop_at < first_rally_at
                          else "sampled_rally_before_stop")
    elif first_stop_at:
        classification = "sampled_stop_only"
    elif first_rally_at:
        classification = "sampled_rally_only"
    else:
        classification = "neither_observed"
    delay = ((first_rally_at - first_stop_at).total_seconds() / 60
             if classification == "sampled_stop_before_rally" else None)
    return {
        "sampled_path_classification": classification,
        "sampled_stop_to_rally_minutes": round(delay, 1) if delay is not None else None,
    }



def _mean(values):
    return round(sum(values) / len(values), 3) if values else None


def _median(values):
    return round(median(values), 3) if values else None


def _next_entry_index(series, signal_index, max_minutes):
    signal_at = series[signal_index][0]
    for index in range(signal_index + 1, len(series)):
        observed_at, quote_at, _, _ = series[index]
        delay = (observed_at - signal_at).total_seconds() / 60
        if delay > max_minutes:
            break
        if delay > 0 and quote_at > signal_at:
            return index
    return None


def _followup(series, entry_index, minutes):
    """First verified observation at/after the horizon inside a bounded window."""
    entry_at, _, entry_price, _ = series[entry_index]
    path = [series[entry_index]]
    latest_quote = entry_at
    for point in series[entry_index + 1:]:
        observed_at, quoted_at, price, _ = point
        age_min = (observed_at - entry_at).total_seconds() / 60
        if age_min > minutes + MAX_OUTCOME_DELAY_MIN:
            break
        if quoted_at <= latest_quote:
            continue
        latest_quote = quoted_at
        path.append(point)
        if age_min < minutes:
            continue
        prices = [point[2] for point in path]
        return {
            **_sampled_threshold_order(path),
            "return_pct": (price / entry_price - 1) * 100,
            "sampled_favorable_pct": (max(prices) / entry_price - 1) * 100,
            "sampled_adverse_pct": (min(prices) / entry_price - 1) * 100,
            "sampled_exit_prices": prices[1:],
            "observed_minutes": round(age_min, 1),
            "end_at": observed_at.isoformat(),
        }
    return None



# The factor comparison extends the existing forward-only lab; it never sets a
# production eligibility or BUY threshold. The 75-minute observation window
# accommodates the scanner's current hourly schedule without claiming a fill.
FACTOR_QUALITY_VERSION = "first-frozen-signal-next-scan-v2"
FACTOR_NEXT_SCAN_MAX_MIN = 75
FACTOR_MIN_PER_SIDE = 30
# Numeric cutoffs reuse already-established early-radar research definitions.
QUALITY_FACTORS = (
    ("higher_lows", "higher_lows", None),
    ("multi_timeframe_alignment", "multi_timeframe_alignment", None),
    ("intraday_breakout", "intraday_breakout", None),
    ("breakout_24h", "breakout_24h", None),
    ("volume_acceleration_3bar_ge_1_5", "volume_acceleration_3bar", 1.5),
    ("cumulative_rvol_same_time_ge_1_3", "cumulative_rvol_same_time", 1.3),
    ("relative_volume_ge_1_5", "relative_volume", 1.5),
)


def _frozen_factor(snapshot, field, cutoff):
    """Unknown is neither positive nor negative; use signal-time features only."""
    features = snapshot.get("features")
    if not isinstance(features, dict):
        return None
    value = features.get(field)
    if cutoff is None:
        return value if isinstance(value, bool) else None
    if isinstance(value, bool) or value is None:
        return None
    try:
        from math import isfinite
        numeric = float(value)
        return numeric >= cutoff if isfinite(numeric) else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_signal_factor_quality(records):
    """Compare frozen factors at the first retained signal, with future quotes only.

    Each asset/symbol contributes at most once to each horizon and factor.
    No-follow-up and unknown feature values are never counted as losses or
    negative features. These are gross sampled outcomes, not executable wins.
    """
    grouped = defaultdict(lambda: {"present": [], "absent": [], "unknown": 0})
    seen_candidates = set()
    first_signals = next_scans = invalid_snapshots = 0
    without_next_scan = 0
    followed_by_horizon = {minutes: 0 for minutes in HORIZONS_MIN}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        asset = str(record.get("asset_type") or "").lower()
        symbol = str(record.get("symbol") or "").upper()
        if asset not in {"stock", "crypto"} or not symbol:
            continue
        key = (asset, symbol)
        if key in seen_candidates:
            continue
        raw_series, rejected = _valid_observations(record)
        series = [point for point in raw_series if _eligible_snapshot(point[3])]
        invalid_snapshots += rejected + len(raw_series) - len(series)
        first = next(
            ((idx, point) for idx, point in enumerate(series)
             if str(point[3].get("shadow_classification") or "").upper() in PHASES),
            None,
        )
        if first is None:
            continue
        seen_candidates.add(key)
        first_signals += 1
        index, point = first
        snapshot = point[3]
        entry_index = _next_entry_index(series, index, FACTOR_NEXT_SCAN_MAX_MIN)
        if entry_index is None:
            without_next_scan += 1
            continue
        next_scans += 1
        for horizon in HORIZONS_MIN:
            outcome = _followup(series, entry_index, horizon)
            if outcome is None:
                continue
            followed_by_horizon[horizon] += 1
            for label, field, cutoff in QUALITY_FACTORS:
                bucket = grouped[(asset, label, horizon)]
                state = _frozen_factor(snapshot, field, cutoff)
                if state is None:
                    bucket["unknown"] += 1
                else:
                    bucket["present" if state else "absent"].append(outcome["return_pct"])

    cohorts = []
    for (asset, label, horizon), data in sorted(grouped.items()):
        yes, no = data["present"], data["absent"]
        enough = len(yes) >= FACTOR_MIN_PER_SIDE and len(no) >= FACTOR_MIN_PER_SIDE
        cohorts.append({
            "asset_type": asset,
            "factor_at_first_signal": label,
            "horizon_minutes": horizon,
            "present_candidates": len(yes),
            "absent_candidates": len(no),
            "unknown_feature_candidates": data["unknown"],
            "positive_gross_followup_pct_present": round(
                100 * sum(x > 0 for x in yes) / len(yes), 2) if yes else None,
            "positive_gross_followup_pct_absent": round(
                100 * sum(x > 0 for x in no) / len(no), 2) if no else None,
            "median_gross_return_pct_present": _median(yes),
            "median_gross_return_pct_absent": _median(no),
            "median_return_difference_pct_points": (
                round(median(yes) - median(no), 3) if enough else None
            ),
            "review_status": (
                "directional_only_not_proven_edge" if enough
                else "insufficient_distinct_candidates_do_not_rank"
            ),
        })
    return {
        "version": FACTOR_QUALITY_VERSION,
        "mode": "shadow_only_no_trades",
        "minimum_distinct_candidates_per_side": FACTOR_MIN_PER_SIDE,
        "next_verified_scan_max_minutes": FACTOR_NEXT_SCAN_MAX_MIN,
        "horizons_minutes": list(HORIZONS_MIN),
        "distinct_candidates_with_first_signal": first_signals,
        "first_signals_with_next_verified_scan": next_scans,
        "first_signals_without_usable_next_scan": without_next_scan,
        "excluded_invalid_snapshots": invalid_snapshots,
        "followed_candidates_by_horizon": followed_by_horizon,
        "cohorts": cohorts,
        "limitations": [
            "Factors are frozen at the first retained research phase, never read from a later outcome scan.",
            "One candidate per asset/symbol, factor, and horizon. Unknown factors and missing outcomes are excluded rather than called failures.",
            "Only independently fresh, eligible quote observations are compared, with a subsequent observation as a hypothetical gross entry.",
            "The 75-minute research window accommodates hourly scans. It is NOT an actionable order-entry or BUY threshold.",
            "Cohort medians are descriptive; at least 30 distinct candidates on EACH side are required before directional comparison.",
            "Groups are not matched for regime, market cap, liquidity, or time; even directional status is not a causal claim.",
            "No modeled fees, slippage, stops, fill guarantees, or win-rate estimate. Do not promote factors without walk-forward evaluation.",
        ],
    }


def build_entry_timing_lab(records):
    """Compare early-research phase timing; never claim those phases are official BUYs.

    A phase contributes only its first retained verified observation per
    candidate. Next-scan entry requires a strictly newer independent quote
    within 30 minutes. Missing follow-up is recorded, never counted as a loss.
    For directional research only: no executable trades, no intrabar prices.
    """
    cohorts = defaultdict(list)
    replay_cohorts = defaultdict(list)
    replay_coverage = defaultdict(Counter)
    entry_coverage = defaultdict(Counter)
    exclusions_by_asset = {"stock": 0, "crypto": 0}
    eligible_candidates = evaluated = excluded = no_next_scan = 0
    examples = []
    path_examples = []
    by_asset = {"stock": 0, "crypto": 0}

    for record in records or []:
        if not isinstance(record, dict):
            continue
        asset = str(record.get("asset_type") or "").lower()
        if asset not in by_asset:
            continue
        series, rejected = _valid_observations(record)
        # Legacy flags can contradict explicit stale/unverified status: reject them.
        truly_eligible = [point for point in series if _eligible_snapshot(point[3])]
        invalid = rejected + len(series) - len(truly_eligible)
        excluded += invalid
        exclusions_by_asset[asset] += invalid
        series = truly_eligible
        if not series:
            continue
        eligible_candidates += 1
        symbol = str(record.get("symbol") or "").upper()[:24]
        seen_phases = set()
        for idx, (signal_at, _, signal_price, snapshot) in enumerate(series):
            phase = str(snapshot.get("shadow_classification") or "").upper()
            if phase not in PHASES or phase in seen_phases:
                continue
            seen_phases.add(phase)
            evaluated += 1
            session = str(snapshot.get("market_session") or "UNKNOWN")
            coverage = entry_coverage[(asset, session)]
            coverage["eligible_phase_events"] += 1
            entry_index = _next_entry_index(series, idx, MAX_NEXT_SCAN_MIN)
            if entry_index is None:
                no_next_scan += 1
                coverage["without_next_fresh_scan"] += 1
                continue
            entry_at, _, entry_price, _ = series[entry_index]
            minutes_to_entry = (entry_at - signal_at).total_seconds() / 60
            by_asset[asset] += 1
            coverage["with_next_fresh_scan"] += 1
            plan_status, plan = saved_plan(snapshot, entry_price)
            replay_coverage[(asset, phase)][plan_status] += 1
            for horizon in HORIZONS_MIN:
                cohort_key = (asset, phase, horizon)
                result = _followup(series, entry_index, horizon)
                if plan is not None:
                    outcomes = replay_cohorts[cohort_key]
                    if result is not None:
                        outcomes.append({
                            **replay_outcome(asset, entry_price, plan, result["sampled_exit_prices"]),
                            "gross_horizon_return_pct": result["return_pct"],
                        })
                if result is None:
                    continue
                del result["sampled_exit_prices"]
                cohorts[cohort_key].append({
                    **result,
                    "signal_at": signal_at.isoformat(),
                    "entry_at": entry_at.isoformat(),
                    "signal_price": signal_price,
                    "entry_price": entry_price,
                    "minutes_to_entry": round(minutes_to_entry, 1),
                    "symbol": symbol,
                })
                if (result["sampled_path_classification"] == "sampled_stop_before_rally"
                        and len(path_examples) < MAX_PATH_EXAMPLES):
                    path_examples.append({
                        "asset_type": asset, "symbol": symbol, "research_phase": phase,
                        "horizon_minutes": horizon,
                        "entry_at": entry_at.isoformat(),
                        "sampled_stop_to_rally_minutes": result["sampled_stop_to_rally_minutes"],
                        "notice": "Observed later stop-to-rally sequence; no intrabar fill/order implied.",
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
            "sampled_stop_before_rally_count": sum(row["sampled_path_classification"] == "sampled_stop_before_rally" for row in outcomes),
            "sampled_rally_before_stop_count": sum(row["sampled_path_classification"] == "sampled_rally_before_stop" for row in outcomes),
            "sampled_stop_only_count": sum(row["sampled_path_classification"] == "sampled_stop_only" for row in outcomes),
            "sampled_rally_only_count": sum(row["sampled_path_classification"] == "sampled_rally_only" for row in outcomes),
            "neither_threshold_observed_count": sum(row["sampled_path_classification"] == "neither_observed" for row in outcomes),
            "median_sampled_stop_to_rally_min": _median([row["sampled_stop_to_rally_minutes"] for row in outcomes if row["sampled_stop_to_rally_minutes"] is not None]),
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
        "excluded_invalid_snapshots_by_asset": exclusions_by_asset,
        "next_scan_samples_by_asset": by_asset,
        "entry_coverage_by_asset_and_session": [
            {"asset_type": asset, "market_session": session,
             "eligible_phase_events": counts["eligible_phase_events"],
             "with_next_fresh_scan": counts["with_next_fresh_scan"],
             "without_next_fresh_scan": counts["without_next_fresh_scan"]}
            for (asset, session), counts in sorted(entry_coverage.items())
        ],
        "cohorts": rows,
        "fee_aware_replay": {
            "version": "frozen-plan-sampled-exits-v1",
            "adverse_exit_bps": ADVERSE_EXIT_BPS,
            "plan_coverage": [
                {"asset_type": asset, "research_phase": phase, **counts}
                for (asset, phase), counts in sorted(replay_coverage.items())
            ],
            "cohorts": [
                {"asset_type": asset, "research_phase": phase, "horizon_minutes": horizon,
                 "missing_horizon_followup": replay_coverage[(asset, phase)]["eligible_plan"] - len(outcomes),
                 **summarize_replay(outcomes)}
                for (asset, phase, horizon), outcomes in sorted(replay_cohorts.items())
            ],
            "assumptions": [
                "Trade levels come from the original signal snapshot. Older snapshots without saved levels are excluded.",
                "Entry uses the first newer verified quote after the signal, subject to the saved entry range.",
                "Stops and targets trigger at sampled quotes. A first target sells half, and a second target or stop sells the remainder.",
                "Each cohort requires a fresh horizon observation. Any remainder is hypothetically sold at that quote.",
                "Both scenarios use the paper fee model and $100 position notional. The adverse case reduces all exit prices by the stated basis points.",
                "Crypto spread assumes the included-commission allowance applies. Post-allowance commission is unconfigured.",
                "Results use retained research snapshots and omit intrabar prices, skipped scans, shared capital and order fills.",
            ],
        },
        "signal_factor_quality": build_signal_factor_quality(records),
        "examples": examples,
        "sampled_path_review_thresholds_pct": {"stop": SAMPLED_STOP_PCT, "rally": SAMPLED_RALLY_PCT},
        "sampled_stop_before_rally_examples": path_examples,
        "limitations": [
            "Research phases are not official BUYs. Gross sampled returns are not actual filled trades, net profits, or a win-rate prediction.",
            "The gross timing cohorts exclude fees and exits. The fee-aware replay reports separate hypothetical outcomes.",
            "A 60/240-minute horizon requires a later fresh observation within 30 minutes; missing outcomes are excluded, not called losses.",
            "The first retained phase event per candidate is measured once. Only the most recent 32 scans may be retained.",
            "Observed sampled highs and lows are not intrabar prices and cannot prove favorable order of stops versus targets.",
            "Stop-before-rally counts order only separate, strictly newer verified quote samples; they cannot prove an executable stop, fill, or intrabar path. A threshold can be crossed unseen between scans.",
            "Missing a sampled threshold never establishes that the price did not cross it between observations. Sampled -3%/+8% are fixed research labels, not Jarvis risk settings.",
            "Phase cohorts from the same candidate overlap. No cross-cohort causal inference or strategy promotion is justified.",
            "No changes to official BUY, Risk-Off, account state, paper capital, alerts, fees, or scan timing.",
        ],
    }
