"""Bounded daily review of the full GitHub Actions historical audit.

This is research output, not a trading input. Render retains its small display
snapshot; complete scanner observations remain in GitHub's paper_tracker.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_DAILY_REVIEWS = 14
MAX_EXAMPLES = 8
SCHEMA_VERSION = 1


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _select(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: row[key] for key in fields if key in row}


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
                   "signals_with_followup", "median_lead_minutes", "median_lead_time_minutes",
                   "review_status", "median_return_pct")
    replay_fields = ("asset_type", "research_phase", "horizon_minutes",
                     "eligible_samples", "executed_samples", "observations", "trades",
                     "net_return_pct", "average_net_return_pct", "win_rate_pct",
                     "sample_status", "review_status", "missing_horizon_followup")
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
            "cohorts": [_select(row, replay_fields) for row in _rows(fees.get("cohorts"))[:MAX_EXAMPLES]],
            "coverage": _rows(fees.get("plan_coverage"))[:MAX_EXAMPLES],
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
            "event_count": len(_rows(account.get("events"))),
            "starting_cash": account.get("starting_cash"),
            "mode": "simulation_only",
        },
        "interpretation_limits": [
            "Missed watchlist moves do not prove safe, executable entries existed.",
            "Outcomes are sampled verified scans; intrabar prices and real fills are unknown.",
            "Small or incomplete cohorts must not be ranked as demonstrated profitable strategies.",
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
