"""Bounded daily review of the full GitHub Actions historical audit.

This is research output, not a trading input. Render retains its small display
snapshot; complete scanner observations remain in GitHub's paper_tracker.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from math import isfinite

from paper_trade_validation import build_paper_trade_validation

MAX_DAILY_REVIEWS = 14
MAX_EXAMPLES = 8
SCHEMA_VERSION = 1


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _select(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: row[key] for key in fields if key in row}


def _replay_cohort(row: dict) -> dict:
    """Keep the actual fee-aware replay statistics, not similarly named missing keys."""
    fields = ("asset_type", "research_phase", "horizon_minutes", "sample_count",
              "missing_horizon_followup", "mean_gross_horizon_return_pct",
              "median_followup_minutes", "positive_gross_horizon_pct")
    cohort = _select(row, fields)
    for scenario in ("observed_quote", "adverse_exit"):
        values = _object(row.get(scenario))
        if values:
            cohort[scenario] = _select(values, ("net_wins", "net_win_rate_pct",
                                                "mean_net_return_pct"))
    return cohort


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number > 0 else None


def _paper_execution_watch(account: dict) -> dict:
    """Flag oversized quote-based paper exits, without rewriting any ledger event."""
    flagged = []
    for trade in _rows(account.get("closed_trades")):
        if trade.get("close_reason") != "TARGET_2" or trade.get("target_1_filled") is True:
            continue
        close = _finite_positive(trade.get("close_price"))
        target = _finite_positive(trade.get("target_2"))
        entry = _finite_positive(trade.get("entry_price"))
        quantity = _finite_positive(trade.get("initial_quantity"))
        if not (close and target and entry and quantity) or close <= target * 1.2:
            continue
        entry_fee = _finite_positive(trade.get("entry_fee")) or 0.0
        paid_fees = _finite_positive(trade.get("fees_paid"))
        recorded_pnl = trade.get("realized_pnl")
        # Approximation only: proportionally scale the *recorded* exit fee,
        # not an invented execution venue or a future Coinbase fee tier.
        capped_pnl = None
        if paid_fees is not None and paid_fees >= entry_fee:
            exit_fee_at_target = (paid_fees - entry_fee) * target / close
            capped_pnl = round(quantity * (target - entry) - entry_fee - exit_fee_at_target, 4)
        flagged.append({
            "symbol": trade.get("symbol"),
            "asset_type": trade.get("asset_type"),
            "recorded_close_reason": trade.get("close_reason"),
            "recorded_close_price": close,
            "saved_target_2": target,
            "quote_overshoot_pct": round(100 * (close / target - 1), 2),
            "recorded_pnl": recorded_pnl,
            "target_2_capped_pnl_illustration": capped_pnl,
            "source": trade.get("price_source"),
            "last_quote_as_of": trade.get("latest_price_as_of"),
            "closed_at": trade.get("closed_at"),
            "coinbase_trade_availability_at_exit": "not_independently_verified",
            "order_book_fill_at_exit": "not_verified",
        })
    flagged.sort(key=lambda row: row["quote_overshoot_pct"], reverse=True)
    return {
        "flagged_trades": flagged[:MAX_EXAMPLES],
        "total_flagged_trades": len(flagged),
        "comparison_method": "full unsplit position at frozen target 2; recorded entry fee plus proportionally scaled recorded exit fee",
        "status": "research_only_not_verified_executable_fills",
    }


def make_research_review(full_export: dict, paper_account: dict | None = None) -> dict:
    """Select evidence-based findings without modifying the audited state."""
    if not isinstance(full_export, dict) or full_export.get("export_type") != "jarvis_historical_opportunity_audit":
        raise ValueError("Expected the complete GitHub Actions historical audit export")
    provenance = _object(full_export.get("provenance"))
    missed = _object(full_export.get("missed_runners"))
    gates = _object(full_export.get("buy_gate_failures"))
    lead = _object(full_export.get("signal_lead_time"))
    timing = _object(full_export.get("entry_timing_lab"))
    fees = _object(timing.get("fee_aware_replay"))
    shadow = _object(full_export.get("gate_combination_shadow"))
    setups = _object(full_export.get("early_warning_cohorts"))
    account = _object(paper_account)
    paper_performance = _object(build_paper_trade_validation(account).get("overall"))
    missed_cases = _object(full_export.get("missed_runner_case_studies"))
    created_at = full_export.get("generated_at")
    if not isinstance(created_at, str) or not created_at[:10]:
        raise ValueError("Historical audit has no generated_at timestamp")
    try:
        parsed_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            raise ValueError("Audit timestamp must include a timezone")
        review_day = parsed_date.astimezone(timezone.utc).date().isoformat()
    except ValueError as exc:
        raise ValueError("Invalid historical audit timestamp") from exc

    missed_fields = ("asset_type", "symbol", "discovery_max_gain_pct", "last_stage",
                     "discovery_max_drawdown_pct", "price_validation_status")
    gate_fields = ("gate", "events", "unique_candidates", "measured", "later_reached_5pct",
                   "later_reached_10pct", "average_max_favorable_pct",
                   "average_max_adverse_pct", "sample_status")
    lead_fields = ("asset_type", "indicator_type", "signal", "threshold_pct",
                   "signals_observed", "signals_with_followup", "later_observed_crossings",
                   "already_crossed_at_first_signal", "median_lead_minutes", "sample_status")
    case_fields = ("found", "symbol", "asset_type", "first_seen", "last_seen",
                   "first_research_signal", "first_fresh_signal", "first_stage_promotion",
                   "first_buy_gate_check", "latest_buy_gate_check",
                   "retained_research_snapshots", "fresh_outcome_snapshots",
                   "discovery_baseline_status", "discovery_first_accepted_price",
                   "discovery_first_quote_at", "discovery_outcome_status",
                   "verified_discovery_max_gain_pct")
    profile_fields = ("name", "asset_type", "relaxed_gates", "regime_gate_relaxed",
                      "newly_qualifying_unique_candidates", "newly_qualifying_with_measured_followup",
                      "sampled_later_reached_10pct", "sampled_adverse_at_least_3pct",
                      "median_max_favorable_pct", "median_max_adverse_pct", "sample_status")
    setup_fields = ("asset_type", "setup", "observations", "runner_count", "runner_rate_pct",
                    "average_5d_return_pct", "sample_status")
    return {
        "date_utc": review_day,
        "generated_at": created_at,
        "source": "full_github_actions_historical_audit",
        "read_only": True,
        "production_effect": "none",
        "scan_run_id": provenance.get("scan_run_id"),
        "last_scan_completed_at": provenance.get("last_scan_completed_at"),
        "coverage": {
            "retained_candidate_records": provenance.get("retained_candidate_records"),
            "official_buy_signal_records": provenance.get("official_buy_signal_records"),
            "eligible_signal_snapshots": lead.get("eligible_snapshots"),
            "excluded_signal_snapshots": lead.get("excluded_snapshots"),
            "eligible_timing_candidates": timing.get("eligible_candidates"),
            "entry_events_without_next_fresh_scan": timing.get("events_without_next_fresh_scan"),
        },
        "missed_runners": {
            "verified_watchlist_runners_without_official_buy": missed.get("total_verified_watchlist_runners"),
            "threshold_pct": missed.get("threshold_pct"),
            "examples": [_select(row, missed_fields) for row in _rows(missed.get("candidates"))[:MAX_EXAMPLES]],
            "case_studies": [_select(row, case_fields)
                             for row in _rows(missed_cases.get("cases"))[:3]],
        },
        "buy_gate_failures": {
            "total_events": gates.get("total_events"),
            "top_blockers": [_select(row, gate_fields) for row in _rows(gates.get("gate_summaries"))[:MAX_EXAMPLES]],
        },
        "early_signal_lead_time": {
            "candidates_with_followup": lead.get("candidates_with_followup"),
            "minimum_for_directional_review": lead.get("min_candidates_for_directional_review"),
            "cohorts": [_select(row, lead_fields) for row in _rows(lead.get("cohorts"))[:MAX_EXAMPLES]],
        },
        "fee_aware_entry_replay": {
            "sampled_only": True,
            "cohorts": [_replay_cohort(row) for row in _rows(fees.get("cohorts"))[:MAX_EXAMPLES]],
            "coverage": _rows(fees.get("plan_coverage"))[:MAX_EXAMPLES],
            "net_return_unit": "percent of $100 modeled notional; sampled hypothetical exits",
        },
        "existing_gate_combinations": {
            "minimum_followup_sample": shadow.get("minimum_unique_followups_for_directional_label"),
            "profiles": [_select(row, profile_fields) for row in _rows(shadow.get("profiles"))[:MAX_EXAMPLES]],
        },
        "early_warning_setups": {
            "minimum_observations": setups.get("minimum_observations"),
            "cohorts": [_select(row, setup_fields) for row in _rows(setups.get("cohorts"))[:MAX_EXAMPLES]],
        },
        "paper_trading": {
            "open_positions": len(_rows(account.get("positions"))),
            "closed_trades": len(_rows(account.get("closed_trades"))),
            "event_count": len(_rows(account.get("ledger"))),
            "starting_cash": account.get("starting_cash"),
            "closed_trade_outcomes": _select(paper_performance,
                ("valid_closed_trades", "closed_trades", "wins", "losses",
                 "win_rate_pct", "net_realized_pnl", "sample_status")),
            "mode": "simulation_only",
        },
        "paper_execution_watch": _paper_execution_watch(account),
        "interpretation_limits": [
            "Missed watchlist moves do not prove safe, executable entries existed.",
            "Outcomes are sampled verified scans; intrabar prices and real fills are unknown.",
            "Small or incomplete cohorts must not be ranked as demonstrated profitable strategies.",
            "A quote exceeding a frozen target is not evidence that a fill at that quote was possible.",
            "Capped target illustrations are not fills or alternative ledger outcomes; recorded fees are proportionally scaled.",
            "The review is observational and cannot change BUY gates, risk, paper trades or live orders.",
        ],
    }


def update_daily_reviews(previous: dict | None, review: dict) -> dict:
    """Keep the last 14 unique UTC review dates, replacing today's latest scan."""
    if not isinstance(review, dict) or not review.get("date_utc"):
        raise ValueError("Review is missing its UTC date")
    existing = _rows(_object(previous).get("daily"))
    by_day = {row["date_utc"]: row for row in existing
              if isinstance(row.get("date_utc"), str) and row.get("date_utc")}
    by_day[review["date_utc"]] = review
    dates = sorted(by_day, reverse=True)[:MAX_DAILY_REVIEWS]
    return {"schema_version": SCHEMA_VERSION,
            "source": "full_github_actions_historical_audit",
            "updated_at": review["generated_at"],
            "latest": review,
            "daily": [by_day[day] for day in dates],
            "retention": "14 latest distinct UTC days, one final scan snapshot per day",
            "read_only": True,
            "production_effect": "none"}


def persist_review(audit_path: Path, account_path: Path, output_path: Path) -> dict:
    export = json.loads(audit_path.read_text(encoding="utf-8"))
    account = json.loads(account_path.read_text(encoding="utf-8"))
    try:
        old = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}
    report = update_daily_reviews(old, make_research_review(export, account))
    output_path.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    output = persist_review(
        Path("jarvis_historical_audit.json"),
        Path("paper_trading_state.json"),
        Path("jarvis_research_review.json"),
    )
    print("Compact daily research review saved for", output["latest"]["date_utc"],
          "(days retained:", len(output["daily"]), ")")
