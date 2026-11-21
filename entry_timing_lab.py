"""Read-only entry-timing research from Jarvis's existing verified scan snapshots.

This lab never emits BUY signals, trades, alert recommendations, or strategy
threshold changes. Hypothetical entry is the *next eligible observation* after
a research label, not the signal price. Subsequent outcomes are sampled gross
returns, not backtested fills or forward win-probability predictions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from signal_lead_time_audit import _positive, _time, _valid_observations
from gate_combination_shadow import SAFETY_GATES
from early_opportunity_radar import _eligible_snapshot
from entry_timing_replay import ADVERSE_EXIT_BPS, frozen_plan, replay_outcome, summarize_replay

MODEL_VERSION = "entry-timing-next-scan-v2"
PHASES = (
    "EARLY WATCH", "EARLY ACCUMULATION",
    "PRE-BREAKOUT WATCH", "BREAKOUT DEVELOPING",
)
HORIZONS_MIN = (60, 240)
MAX_NEXT_SCAN_MIN = 30
MAX_OUTCOME_DELAY_MIN = 30
REPLAY_NEXT_SCAN_MAX_MIN = 75
REPLAY_HORIZON_EARLY_TOLERANCE_MIN = 5
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


def _followup(series, entry_index, minutes, *, early_tolerance_min=0):
    """First newer verified observation in the requested horizon window."""
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
        if age_min < minutes - early_tolerance_min:
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


def _record_fee_replay(asset, series, coverage, cohorts):
    """Replay the first complete frozen plan per phase using forward quotes."""
    seen = set()
    unplanned = {}
    for index, (_, _, _, snapshot) in enumerate(series):
        phase = str(snapshot.get("shadow_classification") or "").upper()
        if phase not in PHASES or phase in seen:
            continue
        status, plan = frozen_plan(snapshot)
        if plan is None:
            if phase not in unplanned or status != "missing_plan":
                unplanned[phase] = status
            continue
        seen.add(phase)
        counts = coverage[(asset, phase)]
        entry_index = _next_entry_index(series, index, REPLAY_NEXT_SCAN_MAX_MIN)
        if entry_index is None:
            counts["without_next_fresh_scan"] += 1
            continue
        entry_price = series[entry_index][2]
        low, high, stop, first_target, second_target = plan
        if not (low <= entry_price <= high and stop < entry_price < first_target):
            counts["quote_outside_plan"] += 1
            continue
        counts["eligible_plan"] += 1
        for horizon in HORIZONS_MIN:
            outcomes = cohorts[(asset, phase, horizon)]
            result = _followup(series, entry_index, horizon, early_tolerance_min=REPLAY_HORIZON_EARLY_TOLERANCE_MIN)
            if result is not None:
                outcomes.append({
                    **replay_outcome(asset, entry_price, (stop, first_target, second_target), result["sampled_exit_prices"]),
                    "gross_horizon_return_pct": result["return_pct"],
                    "observed_minutes": result["observed_minutes"],
                })
    for phase, status in unplanned.items():
        if phase not in seen:
            coverage[(asset, phase)][status] += 1


def _safe_research_quote(point, asset):
    snapshot = point[3]
    failed = snapshot.get("failed_gate_keys")
    return (isinstance(failed, list) and all(isinstance(gate, str) for gate in failed)
            and not SAFETY_GATES.intersection(failed)
            and (asset != "stock" or snapshot.get("data_type") == "live"))


def _earlier_entries(pre_entry, asset):
    """Freeze the first usable early plan, then evaluate only subsequent quotes."""
    early_phases = {"EARLY ACCUMULATION", "PRE-BREAKOUT WATCH"}
    confirmation_phases = early_phases | {"BREAKOUT DEVELOPING"}
    signal = None
    for index, point in enumerate(pre_entry):
        phase = str(point[3].get("shadow_classification") or "").upper()
        if phase not in early_phases or not _safe_research_quote(point, asset):
            continue
        _, plan = frozen_plan(point[3])
        if plan is not None and _in_plan(point[2], plan):
            signal = (index, point, plan)
            break
    if signal is None:
        return None, {}

    index, point, plan = signal
    signal_at = point[0]
    expiry = signal_at + timedelta(minutes=REPLAY_NEXT_SCAN_MAX_MIN)
    prior_quote = signal_at
    crossed_above = False
    entries = {}
    for observation in pre_entry[index + 1:]:
        observed_at, quoted_at, price, snapshot = observation
        if observed_at > expiry:
            break
        if quoted_at <= prior_quote:
            continue
        prior_quote = quoted_at
        if price <= plan[2]:
            break
        if price > plan[1]:
            crossed_above = True
            continue
        if not _in_plan(price, plan) or not _safe_research_quote(observation, asset):
            continue
        if crossed_above:
            entries.setdefault("first_retest", observation)
        elif str(snapshot.get("shadow_classification") or "").upper() in confirmation_phases:
            entries.setdefault("first_verified_confirmation", observation)
    return plan, entries


def _in_plan(price, plan):
    low, high, stop, target_1, _ = plan
    return low <= price <= high and stop < price < target_1


def retain_paired_entry_observations(tracker, account):
    """Keep the existing replay's first paper-entry window when scan history rolls over."""
    trades = [trade for key in ("closed_trades", "positions") for trade in account.get(key, [])
              if isinstance(trade, dict) and _time(trade.get("opened_at"))]
    seen = set()
    for trade in sorted(trades, key=lambda row: _time(row["opened_at"])):
        key = f"{trade.get('asset_type')}:{trade.get('symbol')}"
        record = tracker.get(key)
        if record is None or key in seen:
            continue
        seen.add(key)
        opened = _time(trade["opened_at"])
        start = opened - timedelta(minutes=max(HORIZONS_MIN))
        end = opened + timedelta(minutes=max(HORIZONS_MIN) + MAX_OUTCOME_DELAY_MIN)
        retained = record.get("paired_entry_history") or []
        rows = {row["scan_id"]: row for row in retained if isinstance(row, dict) and row.get("scan_id")}
        for row in record.get("signal_history") or []:
            if not _eligible_snapshot(row) or not row.get("scan_id"):
                continue
            observed = _time(row.get("observed_at"))
            if observed and start <= observed <= end:
                rows.setdefault(row["scan_id"], row)
        if rows:
            record["paired_entry_history"] = list(rows.values())


def _paired_paper_entries(records, paper_account):
    """Compare earlier entries with recorded paper entries on a shared price path."""
    histories = {(str(row.get("asset_type") or "").lower(), str(row.get("symbol") or "").upper()): row
                 for row in (records or []) if isinstance(row, dict)}
    counts = defaultdict(Counter)
    pairs = defaultdict(list)
    trades = [row for key in ("positions", "closed_trades")
              for row in ((paper_account or {}).get(key) or []) if isinstance(row, dict)]
    seen = set()
    for trade in sorted(trades, key=lambda row: str(row.get("opened_at") or "")):
        asset = str(trade.get("asset_type") or "").lower()
        symbol = str(trade.get("symbol") or "").upper()
        if asset not in {"stock", "crypto"} or not symbol or (asset, symbol) in seen:
            continue
        seen.add((asset, symbol))
        strategy = str(trade.get("strategy") or "official_buy")
        regime = str(trade.get("market_regime_at_entry") or "UNKNOWN")
        group = (asset, strategy, regime)
        coverage = counts[group]
        coverage["recorded_paper_entries"] += 1
        opened = _time(trade.get("opened_at"))
        quoted = _time(trade.get("entry_price_as_of"))
        price = _positive(trade.get("entry_price"))
        exits = tuple(_positive(trade.get(key)) for key in ("stop_loss", "target_1", "target_2"))
        if not (opened and quoted and price and all(exits)
                and 0 <= (opened - quoted).total_seconds() <= 900
                and exits[0] < price < exits[1] <= exits[2]):
            coverage["unusable_paper_entry"] += 1
            continue
        record = histories.get((asset, symbol), {})
        snapshots = [*(record.get("paired_entry_history") or []), *(record.get("signal_history") or [])]
        history, _ = _valid_observations({**record, "signal_history": snapshots})
        history = [point for point in history if _eligible_snapshot(point[3])]
        lookback_start = opened - timedelta(minutes=max(HORIZONS_MIN))
        pre_entry = [point for point in history if lookback_start <= point[0] < opened]
        if not pre_entry:
            coverage["without_earlier_verified_observation"] += 1
            continue
        plan, entries = _earlier_entries(pre_entry, asset)
        if plan is None:
            coverage["without_earlier_frozen_plan"] += 1
            continue
        if not entries:
            coverage["without_earlier_trigger"] += 1
            continue

        baseline = [(opened, quoted, price, {}), *(point for point in history if point[0] > opened)]
        for trigger, entry in entries.items():
            coverage[trigger + "_entries"] += 1
            lead = (opened - entry[0]).total_seconds() / 60
            earlier_prices = []
            quote_cutoff = entry[0]
            for observed_at, quote_at, observed_price, _ in [*pre_entry, baseline[0]]:
                if observed_at > entry[0] and quote_cutoff < quote_at <= opened:
                    earlier_prices.append(observed_price)
                    quote_cutoff = quote_at
            for horizon in HORIZONS_MIN:
                followup = _followup(baseline, 0, horizon,
                                     early_tolerance_min=REPLAY_HORIZON_EARLY_TOLERANCE_MIN)
                if followup is None:
                    coverage[trigger + "_missing_followup_" + str(horizon)] += 1
                    continue
                prices = followup["sampled_exit_prices"]
                current = replay_outcome(asset, price, exits, prices)
                earlier = replay_outcome(asset, entry[2], plan[2:], earlier_prices + prices)
                pairs[(*group, trigger, horizon)].append({
                    "lead": lead, "price_savings": (1 - entry[2] / price) * 100,
                    "current": current, "earlier": earlier,
                })
    return {
        "mode": "read_only_paired_replay",
        "coverage": [
            {"asset_type": asset, "paper_strategy": strategy, "paper_entry_regime": regime, **coverage}
            for (asset, strategy, regime), coverage in sorted(counts.items())
        ],
        "cohorts": [
            {
                "asset_type": asset, "paper_strategy": strategy, "paper_entry_regime": regime,
                "earlier_trigger": trigger, "horizon_minutes_after_paper_entry": horizon,
                "paired_candidates": len(rows),
                "median_minutes_earlier": _median([row["lead"] for row in rows]),
                "median_entry_price_savings_pct": _median([row["price_savings"] for row in rows]),
                "review_status": ("directional_only" if len(rows) >= MIN_DIRECTIONAL_SAMPLE
                                  else "insufficient_sample_do_not_rank"),
                **{
                    label: {
                        "sampled_stops": sum(row[label]["exit_reason"] == "STOP" for row in rows),
                        "mean_net_return_pct": _mean([row[label]["observed_quote"] for row in rows]),
                        "mean_net_return_adverse_exit_pct": _mean([row[label]["adverse_exit"] for row in rows]),
                    }
                    for label in ("current", "earlier")
                },
            }
            for (asset, strategy, regime, trigger, horizon), rows in sorted(pairs.items())
        ],
        "assumptions": [
            "Compare the first recorded paper entry per symbol with each earlier trigger separately on the same candidate.",
            "Use the first complete plan frozen at a safe early accumulation or pre-breakout observation in the four hours before the recorded entry.",
            "Confirmation requires a newer quote still in range with an early or developing-breakout label. A retest requires a sampled move above the range and then a return into it.",
            "The research plan expires after 75 minutes or an observed stop breach. Each alternative must occur before the recorded entry.",
            "Both sides use the same follow-up endpoint, with their own frozen stops and targets and the existing fee, spread and adverse exit models. Missing follow-up is excluded.",
            "Cohorts are conditioned on eventual paper entries and grouped by the recorded entry regime. They do not measure false positives across the full watchlist or qualify trades under current rules.",
        ],
    }


def build_entry_timing_lab(records, *, paper_account=None):
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
        _record_fee_replay(asset, series, replay_coverage, replay_cohorts)
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
            for horizon in HORIZONS_MIN:
                cohort_key = (asset, phase, horizon)
                result = _followup(series, entry_index, horizon)
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
            "version": "frozen-plan-sampled-exits-v2",
            "adverse_exit_bps": ADVERSE_EXIT_BPS,
            "max_next_scan_minutes": REPLAY_NEXT_SCAN_MAX_MIN,
            "horizon_early_tolerance_minutes": REPLAY_HORIZON_EARLY_TOLERANCE_MIN,
            "horizon_late_tolerance_minutes": MAX_OUTCOME_DELAY_MIN,
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
                "For each candidate and phase, use the first retained verified signal snapshot with a complete saved plan. Earlier unplanned signals supply no levels.",
                "Entry uses the first newer verified quote within 75 minutes after that signal, subject to the saved entry range.",
                "Stops and targets trigger at sampled quotes. A first target sells half, and a second target or stop sells the remainder.",
                "Horizon follow-up uses the first newer verified quote observed from 5 minutes before to 30 minutes after the nominal horizon. Any remainder is hypothetically sold there.",
                "Both scenarios use the paper fee model and $100 position notional. The adverse case reduces all exit prices by the stated basis points.",
                "Crypto spread assumes the included-commission allowance applies. Post-allowance commission is unconfigured.",
                "Results use retained research snapshots and omit intrabar prices, skipped scans, shared capital and order fills.",
            ],
        },
        "paired_paper_entries": _paired_paper_entries(records, paper_account),
        "signal_factor_quality": build_signal_factor_quality(records),
        "examples": examples,
        "sampled_path_review_thresholds_pct": {"stop": SAMPLED_STOP_PCT, "rally": SAMPLED_RALLY_PCT},
        "sampled_stop_before_rally_examples": path_examples,
        "limitations": [
            "Research phases are not official BUYs. Gross sampled returns are not actual filled trades, net profits, or a win-rate prediction.",
            "The gross timing cohorts exclude fees and exits. The fee-aware replay reports separate hypothetical outcomes.",
            "The gross timing cohorts require a later fresh observation within 30 minutes after the 60/240-minute horizon. Missing outcomes are excluded.",
            "The first retained phase event per candidate is measured once. Only the most recent 32 scans may be retained.",
            "Observed sampled highs and lows are not intrabar prices and cannot prove favorable order of stops versus targets.",
            "Stop-before-rally counts order only separate, strictly newer verified quote samples; they cannot prove an executable stop, fill, or intrabar path. A threshold can be crossed unseen between scans.",
            "Missing a sampled threshold never establishes that the price did not cross it between observations. Sampled -3%/+8% are fixed research labels, not Jarvis risk settings.",
            "Phase cohorts from the same candidate overlap. No cross-cohort causal inference or strategy promotion is justified.",
            "No changes to official BUY, Risk-Off, account state, paper capital, alerts, fees, or scan timing.",
        ],
    }
