#!/usr/bin/env python3
"""Read-only historical opportunity audit for Jarvis candidate cohorts.

This script reads saved scanner history and emits a JSON + Markdown report.
It does not modify scanner state, scores, thresholds, or paper/live trading.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_snapshot(record: dict) -> dict:
    value = record.get("early_runner_initial_feature_snapshot")
    return value if isinstance(value, dict) else {}


def get_outcome(record: dict, horizon: str = "5d") -> dict | None:
    outcomes = record.get("early_runner_outcomes")
    if not isinstance(outcomes, dict):
        return None
    value = outcomes.get(horizon)
    return value if isinstance(value, dict) and number(value.get("return_pct")) is not None else None


def cohort_eligible(record: dict) -> bool:
    if record.get("asset_type") not in ("stock", "crypto"):
        return False
    if not record.get("early_runner_snapshot_at") or not get_snapshot(record):
        return False
    # Match the production research cohort rule: stock snapshots must have
    # started from a fresh, timestamped, session-aware observation.
    if record.get("asset_type") == "stock":
        return record.get("session_aware_tracking") is True
    return True


def outcome_summary(rows: list[dict]) -> dict:
    values = [number(row.get("outcome", {}).get("return_pct")) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return {"observations": 0}
    favorable = [number(row.get("record", {}).get("research_max_favorable_pct")) for row in rows]
    adverse = [number(row.get("record", {}).get("research_max_adverse_pct")) for row in rows]
    favorable = [x for x in favorable if x is not None]
    adverse = [x for x in adverse if x is not None]
    return {
        "observations": len(values),
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 1),
        "runner_10pct_rate_pct": round(sum(value >= 10 for value in values) / len(values) * 100, 1),
        "runner_20pct_rate_pct": round(sum(value >= 20 for value in values) / len(values) * 100, 1),
        "average_return_pct": round(statistics.mean(values), 2),
        "median_return_pct": round(statistics.median(values), 2),
        "average_max_favorable_pct": round(statistics.mean(favorable), 2) if favorable else None,
        "average_max_adverse_pct": round(statistics.mean(adverse), 2) if adverse else None,
    }


def feature_match(snapshot: dict, name: str) -> bool | None:
    """Return None for unavailable data; never count missing features as false."""
    features = snapshot.get("crqs_features") if isinstance(snapshot.get("crqs_features"), dict) else {}
    def ge(value, threshold):
        parsed = number(value)
        return None if parsed is None else parsed >= threshold
    def gt(value, threshold):
        parsed = number(value)
        return None if parsed is None else parsed > threshold
    checks = {
        "stock_volume_rvol_1_5x": lambda: ge(snapshot.get("cumulative_rvol_same_time"), 1.5),
        "stock_volume_acceleration_1_8x": lambda: ge(snapshot.get("volume_acceleration_3bar"), 1.8),
        "stock_float_rotation_20pct": lambda: ge(snapshot.get("float_rotation_pct"), 20),
        "stock_premarket_rvol_1_5x": lambda: ge(snapshot.get("premarket_rvol_same_time"), 1.5),
        "stock_higher_highs_lows": lambda: snapshot.get("higher_highs_higher_lows") is True,
        "stock_premarket_reclaim": lambda: snapshot.get("premarket_high_reclaimed") is True,
        "stock_catalyst": lambda: bool(snapshot.get("catalyst_labels")),
        "stock_breakout_nearby": lambda: (
            number(snapshot.get("breakout_proximity_pct")) is not None
            and -0.5 <= number(snapshot.get("breakout_proximity_pct")) <= 3.0
        ),
        "stock_volatility_compressed": lambda: (
            number(snapshot.get("volatility_compression_percentile")) is not None
            and number(snapshot.get("volatility_compression_percentile")) <= 20
        ),
        "crypto_hourly_momentum_positive": lambda: gt(snapshot.get("hourly_change_pct"), 0),
        "crypto_24h_momentum_3pct": lambda: ge(snapshot.get("daily_change_pct"), 3),
        "crypto_relative_strength_vs_btc_positive": lambda: gt(snapshot.get("crqs_relative_strength_vs_btc_24h_pct"), 0),
        "crypto_volume_consistency_70pct": lambda: ge(features.get("volume_consistency_6h"), 0.70),
        "crypto_higher_lows": lambda: features.get("higher_lows") is True or snapshot.get("higher_lows") is True,
        "crypto_breakout_24h": lambda: snapshot.get("breakout_24h") is True,
        "crypto_above_24h_average": lambda: snapshot.get("above_24h_average") is True,
        "crypto_accumulation_50": lambda: ge(snapshot.get("accumulation_signal_score"), 50),
        "crypto_liquidity_sweep": lambda: snapshot.get("liquidity_sweep_detected") is True,
        "crypto_breakout_nearby": lambda: (
            number(snapshot.get("breakout_proximity_pct")) is not None
            and -0.5 <= number(snapshot.get("breakout_proximity_pct")) <= 3.0
        ),
        "crypto_volatility_compressed": lambda: (
            number(snapshot.get("volatility_compression_percentile")) is not None
            and number(snapshot.get("volatility_compression_percentile")) <= 20
        ),
    }
    try:
        # Guard against absent fields explicitly so unknown coverage stays unknown.
        field_groups = {
            "stock_volume_rvol_1_5x": ("cumulative_rvol_same_time",),
            "stock_volume_acceleration_1_8x": ("volume_acceleration_3bar",),
            "stock_float_rotation_20pct": ("float_rotation_pct",),
            "stock_premarket_rvol_1_5x": ("premarket_rvol_same_time",),
            "stock_higher_highs_lows": ("higher_highs_higher_lows",),
            "stock_premarket_reclaim": ("premarket_high_reclaimed",),
            "stock_catalyst": ("catalyst_labels",),
            "stock_breakout_nearby": ("breakout_proximity_pct",),
            "stock_volatility_compressed": ("volatility_compression_percentile",),
            "crypto_hourly_momentum_positive": ("hourly_change_pct",),
            "crypto_24h_momentum_3pct": ("daily_change_pct",),
            "crypto_relative_strength_vs_btc_positive": ("crqs_relative_strength_vs_btc_24h_pct",),
            "crypto_volume_consistency_70pct": ("crqs_features",),
            "crypto_higher_lows": ("crqs_features", "higher_lows"),
            "crypto_breakout_24h": ("breakout_24h",),
            "crypto_above_24h_average": ("above_24h_average",),
            "crypto_accumulation_50": ("accumulation_signal_score",),
            "crypto_liquidity_sweep": ("liquidity_sweep_detected",),
            "crypto_breakout_nearby": ("breakout_proximity_pct",),
            "crypto_volatility_compressed": ("volatility_compression_percentile",),
        }
        for path_key in field_groups[name]:
            if path_key == "crqs_features":
                if not isinstance(snapshot.get("crqs_features"), dict):
                    return None
            elif path_key == "higher_lows":
                if path_key not in features:
                    return None
            elif path_key not in snapshot or snapshot.get(path_key) is None:
                return None
        result = checks[name]()
        return bool(result) if result is not None else None
    except (TypeError, ValueError):
        return None


def failed_gate_labels(snapshot: dict) -> list[str]:
    readiness = snapshot.get("buy_readiness")
    if not isinstance(readiness, dict):
        return []
    labels = []
    for key in ("failed_gates", "blocking_reasons", "missing_requirements", "reasons"):
        value = readiness.get(key)
        if isinstance(value, list):
            labels.extend(str(item) for item in value if item)
    gates = readiness.get("gates")
    if isinstance(gates, dict):
        for key, value in gates.items():
            if value is False or (isinstance(value, dict) and value.get("passed") is False):
                labels.append(str(key))
    return list(dict.fromkeys(labels))



def discovery_analysis(records: list[dict], asset: str) -> dict:
    """Analyze verified first-recommendation baselines and retained price observations.

    This is a high-water-mark opportunity study, not an executable backtest.
    """
    eligible = [
        record for record in records
        if record.get("asset_type") == asset
        and number(record.get("initial_price")) is not None
        and number(record.get("initial_price")) > 0
        and record.get("discovery_return_status") == "verified"
        and number(record.get("max_gain_pct")) is not None
    ]
    gains = [number(record.get("max_gain_pct")) for record in eligible]
    drawdowns = [number(record.get("max_drawdown_pct")) for record in eligible]
    stage_groups = {}
    for record in eligible:
        stage = str(record.get("first_grade") or "UNKNOWN")
        stage_groups.setdefault(stage, []).append(record)
    stage_stats = {}
    for stage, group in sorted(stage_groups.items()):
        stage_gains = [number(row.get("max_gain_pct")) for row in group]
        stage_stats[stage] = {
            "n": len(group),
            "runner_10pct_rate_pct": round(sum(x >= 10 for x in stage_gains) / len(group) * 100, 1),
            "runner_20pct_rate_pct": round(sum(x >= 20 for x in stage_gains) / len(group) * 100, 1),
            "median_observed_max_gain_pct": round(statistics.median(stage_gains), 2),
        }

    def stage_at(record: dict, timestamp: str) -> str | None:
        history = record.get("grade_history") or []
        current = record.get("first_grade")
        for change in sorted(history, key=lambda item: str(item.get("at") or "")):
            if str(change.get("at") or "") <= timestamp:
                current = change.get("to") or current
        return current

    top = []
    missed = []
    for record in sorted(eligible, key=lambda row: number(row.get("max_gain_pct")) or -999, reverse=True):
        history = sorted(record.get("price_history") or [], key=lambda item: str(item.get("at") or ""))
        crossings = {}
        for threshold in (5, 10, 20):
            crossing = next(
                (item for item in history if number(item.get("return_pct")) is not None
                 and number(item.get("return_pct")) >= threshold),
                None,
            )
            if crossing:
                crossings[str(threshold)] = {
                    "at": crossing.get("at"),
                    "return_pct": crossing.get("return_pct"),
                    "stage_at_cross": stage_at(record, str(crossing.get("at") or "")),
                }
        grade_history = record.get("grade_history") or []
        ever_buy = any(str(item.get("to") or "").upper() == "BUY" for item in grade_history)
        row = {
            "symbol": record.get("symbol"),
            "first_seen": record.get("first_seen"),
            "initial_price": record.get("initial_price"),
            "first_stage": record.get("first_grade"),
            "latest_stage": record.get("last_stage"),
            "observed_max_gain_pct": record.get("max_gain_pct"),
            "observed_max_drawdown_pct": record.get("max_drawdown_pct"),
            "current_return_pct": record.get("return_pct"),
            "milestones": record.get("milestones") or {},
            "price_observation_count": len(history),
            "retained_history_start": history[0].get("at") if history else None,
            "retained_history_end": history[-1].get("at") if history else None,
            "first_5pct_crossing": crossings.get("5"),
            "first_10pct_crossing": crossings.get("10"),
            "first_20pct_crossing": crossings.get("20"),
            "ever_reached_buy_stage_in_grade_history": ever_buy,
            "grade_history": grade_history[-20:],
        }
        top.append(row)
        if number(record.get("max_gain_pct")) >= 20 and not ever_buy:
            missed.append(row)
    return {
        "verified_recommendation_count": len(eligible),
        "runner_10pct_count": sum(value >= 10 for value in gains),
        "runner_20pct_count": sum(value >= 20 for value in gains),
        "median_observed_max_gain_pct": round(statistics.median(gains), 2) if gains else None,
        "average_observed_max_gain_pct": round(statistics.mean(gains), 2) if gains else None,
        "average_observed_max_drawdown_pct": round(statistics.mean([x for x in drawdowns if x is not None]), 2)
            if any(x is not None for x in drawdowns) else None,
        "first_stage_performance": stage_stats,
        "top_observed_runners": top[:25],
        "potential_missed_20pct_runners": missed[:50],
        "definition": "Verified first-recommendation baseline with observed high-water mark; not a trade return or proof that a BUY was executable.",
    }

def analyze(tracker: dict, paper_state: dict, buy_history: Any) -> dict:
    records = list(tracker.values()) if isinstance(tracker, dict) else (
        tracker if isinstance(tracker, list) else []
    )
    valid_records = [r for r in records if isinstance(r, dict) and cohort_eligible(r)]
    report: dict[str, Any] = {
        "audit_type": "read_only_historical_opportunity_audit",
        "data_scope": "Saved Jarvis candidate tracker; no live market calls and no state changes",
        "methodology": {
            "primary_cohort": "Frozen early-runner feature snapshots with completed 5-day outcome",
            "stock_eligibility": "Session-aware tracking required; legacy/stale stock cohorts excluded",
            "crypto_eligibility": "Frozen verified-price feature snapshot required",
            "outcome_note": "First completed scan at or after 5 days, not an exact-time fill",
            "feature_note": "Missing feature values are excluded from that feature's denominator",
            "not_a_backtest": "No intrabar execution simulation, fees, slippage, or order fills are inferred",
        },
        "tracker_record_count": len(records),
        "eligible_snapshot_count": len(valid_records),
        "assets": {},
        "discovery_performance": {},
        "top_runners": {},
        "gate_failure_evidence": {},
        "paper_trading": {},
        "official_buy_history_count": len(buy_history) if isinstance(buy_history, list) else (
            len(buy_history) if isinstance(buy_history, dict) else 0
        ),
        "limitations": [
            "Historical candidate tracking is observational and may have survivorship or scan-coverage bias.",
            "The 5-day outcome is sampled at the first completed scan after the horizon, not a guaranteed entry or exit.",
            "Maximum favorable/adverse excursions are observed research samples, not executable trade returns.",
            "Historical BUY gate blockers are reported only where the frozen snapshot retained structured gate details.",
            "No strategy threshold or production behavior is modified by this audit.",
        ],
    }
    feature_names = {
        "stock": [
            "stock_volume_rvol_1_5x", "stock_volume_acceleration_1_8x",
            "stock_float_rotation_20pct", "stock_premarket_rvol_1_5x",
            "stock_higher_highs_lows", "stock_premarket_reclaim", "stock_catalyst",
            "stock_breakout_nearby", "stock_volatility_compressed",
        ],
        "crypto": [
            "crypto_hourly_momentum_positive", "crypto_24h_momentum_3pct",
            "crypto_relative_strength_vs_btc_positive", "crypto_volume_consistency_70pct",
            "crypto_higher_lows", "crypto_breakout_24h", "crypto_above_24h_average",
            "crypto_accumulation_50", "crypto_liquidity_sweep", "crypto_breakout_nearby",
            "crypto_volatility_compressed",
        ],
    }
    for asset in ("stock", "crypto"):
        cohort = [r for r in valid_records if r.get("asset_type") == asset]
        complete = [{"record": r, "snapshot": get_snapshot(r), "outcome": get_outcome(r)}
                    for r in cohort if get_outcome(r)]
        asset_report = {
            "snapshot_count": len(cohort),
            "completed_5d_count": len(complete),
            "five_day_outcomes": outcome_summary(complete),
            "feature_cohorts": {},
        }
        for name in feature_names[asset]:
            matched, unmatched = [], []
            for row in complete:
                value = feature_match(row["snapshot"], name)
                if value is True:
                    matched.append(row)
                elif value is False:
                    unmatched.append(row)
            matched_stats = outcome_summary(matched)
            unmatched_stats = outcome_summary(unmatched)
            asset_report["feature_cohorts"][name] = {
                "feature_present_n": matched_stats.get("observations", 0),
                "feature_absent_n": unmatched_stats.get("observations", 0),
                "feature_present": matched_stats,
                "feature_absent": unmatched_stats,
                "runner_20pct_uplift_vs_absent_pp": (
                    round(matched_stats["runner_20pct_rate_pct"] - unmatched_stats["runner_20pct_rate_pct"], 1)
                    if matched_stats.get("observations", 0) and unmatched_stats.get("observations", 0)
                    else None
                ),
                "research_status": (
                    "enough data for further validation" if matched_stats.get("observations", 0) >= 20
                    and unmatched_stats.get("observations", 0) >= 20
                    else "exploratory only — small or incomplete cohort"
                ),
            }
        report["assets"][asset] = asset_report
        report["discovery_performance"][asset] = discovery_analysis(records, asset)
        ranked = sorted(
            complete,
            key=lambda row: number(row["outcome"].get("return_pct")) or -999,
            reverse=True,
        )
        top = []
        for row in ranked[:20]:
            record, snapshot = row["record"], row["snapshot"]
            top.append({
                "symbol": record.get("symbol"),
                "asset_type": asset,
                "snapshot_at": record.get("early_runner_snapshot_at"),
                "entry_price": record.get("early_runner_entry_price"),
                "first_stage": record.get("first_grade"),
                "last_stage": record.get("last_stage"),
                "score_at_snapshot": snapshot.get("score"),
                "early_runner_research_score": snapshot.get("early_runner_research_score"),
                "outcome_5d_pct": row["outcome"].get("return_pct"),
                "max_favorable_pct": record.get("research_max_favorable_pct"),
                "max_adverse_pct": record.get("research_max_adverse_pct"),
                "feature_matches": snapshot.get("early_runner_signal_matches") or snapshot.get("early_warning_matches") or [],
                "buy_gate_blockers_recorded": failed_gate_labels(snapshot),
                "grade_history": record.get("grade_history") or [],
            })
        report["top_runners"][asset] = top
        blockers = Counter(
            label for row in complete for label in failed_gate_labels(row["snapshot"])
        )
        report["gate_failure_evidence"][asset] = {
            "records_with_structured_blockers": sum(bool(failed_gate_labels(row["snapshot"])) for row in complete),
            "blocker_counts": dict(blockers.most_common(20)),
            "note": "No blocker count means the frozen historical snapshots did not retain a structured gate breakdown; it does not mean no gates failed.",
        }

    closed = paper_state.get("closed_trades", []) if isinstance(paper_state, dict) else []
    report["paper_trading"] = {
        "mode": paper_state.get("mode") if isinstance(paper_state, dict) else None,
        "starting_cash": paper_state.get("starting_cash") if isinstance(paper_state, dict) else None,
        "cash": paper_state.get("cash") if isinstance(paper_state, dict) else None,
        "closed_trade_count": len(closed) if isinstance(closed, list) else 0,
        "closed_trades": closed[-50:] if isinstance(closed, list) else [],
        "note": "Paper trades are summarized separately and are not mixed into candidate-cohort returns.",
    }
    return report


def render_markdown(report: dict) -> str:
    lines = [
        "# Jarvis Historical Opportunity Audit",
        "",
        "**Read-only research output.** No scanner state, strategy, risk setting, BUY gate, paper capital, or schedule was changed.",
        "",
        f"- Tracker records: {report['tracker_record_count']}",
        f"- Eligible frozen snapshots: {report['eligible_snapshot_count']}",
        f"- Official BUY history records: {report['official_buy_history_count']}",
        "",
        "## Verified first-recommendation opportunity audit",
        "",
        "This section uses verified first-recommendation prices and observed high-water marks. It is not an executable backtest.",
        ""
        "## Method and cautions",
        "",
        report["methodology"]["primary_cohort"],
        "",
        report["methodology"]["stock_eligibility"],
        "",
        report["methodology"]["crypto_eligibility"],
        "",
        report["methodology"]["outcome_note"],
        "",
    ]
    for asset in ("stock", "crypto"):
        discovery = report["discovery_performance"][asset]
        lines += [
            f"### {asset.title()} verified discovery performance",
            "",
            f"- Verified first-recommendation baselines: {discovery['verified_recommendation_count']}",
            f"- Observed 10%+ runners: {discovery['runner_10pct_count']}",
            f"- Observed 20%+ runners: {discovery['runner_20pct_count']}",
            f"- Median observed max gain: {discovery['median_observed_max_gain_pct']}%",
            f"- Average observed max drawdown: {discovery['average_observed_max_drawdown_pct']}%",
            "",
            "| First stage | n | 10%+ max-gain rate | 20%+ max-gain rate | Median max gain |",
            "|---|---:|---:|---:|---:|",
        ]
        for stage, stats in discovery["first_stage_performance"].items():
            lines.append(f"| {stage} | {stats['n']} | {stats['runner_10pct_rate_pct']}% | {stats['runner_20pct_rate_pct']}% | {stats['median_observed_max_gain_pct']}% |")
        lines += [
            "",
            "#### Potential missed 20% runners (no BUY stage recorded)",
            "",
            "| Symbol | First seen | First stage | Max gain | Max drawdown | First 10% crossing stage |",
            "|---|---|---|---:|---:|---|",
        ]
        for row in discovery["potential_missed_20pct_runners"][:20]:
            crossing = row.get("first_10pct_crossing") or {}
            lines.append(f"| {row.get('symbol')} | {row.get('first_seen')} | {row.get('first_stage')} | {row.get('observed_max_gain_pct')}% | {row.get('observed_max_drawdown_pct')}% | {crossing.get('stage_at_cross', 'not retained')} |")
        lines += [""]
        data = report["assets"][asset]
        stats = data["five_day_outcomes"]
        lines += [
            f"## {asset.title()} opportunity results",
            "",
            f"- Eligible snapshots: {data['snapshot_count']}",
            f"- Completed 5-day observations: {data['completed_5d_count']}",
            f"- Win rate: {stats.get('win_rate_pct', 'n/a')}%",
            f"- 10%+ runners: {stats.get('runner_10pct_rate_pct', 'n/a')}%",
            f"- 20%+ runners: {stats.get('runner_20pct_rate_pct', 'n/a')}%",
            f"- Average / median 5-day return: {stats.get('average_return_pct', 'n/a')}% / {stats.get('median_return_pct', 'n/a')}%",
            "",
            "### Early-feature comparison",
            "",
            "| Feature | Present n | 20%+ runner rate | Absent n | Absent runner rate | Uplift (pp) | Status |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for name, feature in data["feature_cohorts"].items():
            present, absent = feature["feature_present"], feature["feature_absent"]
            lines.append(
                f"| {name} | {feature['feature_present_n']} | "
                f"{present.get('runner_20pct_rate_pct', 'n/a')}% | "
                f"{feature['feature_absent_n']} | "
                f"{absent.get('runner_20pct_rate_pct', 'n/a')}% | "
                f"{feature.get('runner_20pct_uplift_vs_absent_pp', 'n/a')} | "
                f"{feature['research_status']} |"
            )
        lines += ["", "### Top observed runners", "", "| Symbol | Snapshot | Stage | Snapshot score | 5d return | Max favorable | Max adverse |", "|---|---|---|---:|---:|---:|---:|"]
        for row in report["top_runners"][asset][:15]:
            lines.append(
                f"| {row.get('symbol')} | {row.get('snapshot_at')} | {row.get('first_stage')} | "
                f"{row.get('score_at_snapshot', 'n/a')} | {row.get('outcome_5d_pct')}% | "
                f"{row.get('max_favorable_pct', 'n/a')}% | {row.get('max_adverse_pct', 'n/a')}% |"
            )
        lines += ["", "### Recorded BUY-gate blockers", "", str(report["gate_failure_evidence"][asset]), ""]
    lines += [
        "## Paper-trading context",
        "",
        f"- Mode: {report['paper_trading'].get('mode')}",
        f"- Closed trades: {report['paper_trading'].get('closed_trade_count')}",
        "- Paper trades are not mixed with candidate cohort outcomes.",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", type=Path, default=Path("paper_tracker.json"))
    parser.add_argument("--paper-state", type=Path, default=Path("paper_trading_state.json"))
    parser.add_argument("--buy-history", type=Path, default=Path("buy_signal_history.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("historical_opportunity_audit"))
    args = parser.parse_args()

    tracker = load_json(args.tracker, {})
    paper_state = load_json(args.paper_state, {})
    buy_history = load_json(args.buy_history, [])
    report = analyze(tracker, paper_state, buy_history)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "audit_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.output_dir / "audit_report.md").write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))


if __name__ == "__main__":
    main()
