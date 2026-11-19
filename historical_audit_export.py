"""Compact, read-only historical audit export for Jarvis Intelligence.

The export deliberately omits raw candidate history and account state. It exposes
bounded research summaries already computed from retained, verified observations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from runner_intelligence import build_runner_intelligence_report


EXPORT_VERSION = "1.0"
MAX_MISSED_RUNNERS = 50
MAX_GATE_EVENTS = 100
MAX_RADAR_CANDIDATES = 30


def build_historical_audit_export(
    records: list[dict[str, Any]],
    buy_signals: list[dict[str, Any]] | None = None,
    *,
    last_scan_completed_at: str | None = None,
    scan_run_id: str | int | None = None,
    synced_at: str | None = None,
    history_source: str = "unknown",
) -> dict[str, Any]:
    """Return a bounded summary; never mutate inputs or expose raw account state."""
    values = [row for row in (records or []) if isinstance(row, dict)]
    report = build_runner_intelligence_report(values, buy_signals or [])
    missed = report["missed_runner_tracker"]
    gates = report["buy_gate_failure_tracker"]
    radar = report["early_warning_radar"]
    cohorts = report["signal_quality_by_setup"]

    missed_rows = missed.get("candidates", [])
    gate_rows = gates.get("recent_events", [])
    radar_rows = radar.get("candidates", [])
    cohort_rows = cohorts.get("cohorts", [])

    return {
        "export_type": "jarvis_historical_opportunity_audit",
        "export_version": EXPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "production_effect": "none",
        "provenance": {
            "last_scan_completed_at": last_scan_completed_at,
            "scan_run_id": str(scan_run_id) if scan_run_id is not None else None,
            "synced_at": synced_at,
            "history_source": history_source,
            "retained_candidate_records": len(values),
            "official_buy_signal_records": len(buy_signals or []),
        },
        "definitions": {
            "missed_runner": missed.get("definition"),
            "buy_gate_failure": gates.get("definition"),
            "signal_quality": cohorts.get("definition"),
            "outcomes_are_sampled": True,
            "causal_claims": False,
        },
        "missed_runners": {
            "threshold_pct": missed.get("threshold_pct"),
            "total_verified_watchlist_runners": missed.get("verified_watchlist_runners", 0),
            "included_candidates_by_asset": {
                "stocks": sum(1 for row in missed_rows[:MAX_MISSED_RUNNERS] if row.get("asset_type") == "stock"),
                "crypto": sum(1 for row in missed_rows[:MAX_MISSED_RUNNERS] if row.get("asset_type") == "crypto"),
            },
            "candidates": missed_rows[:MAX_MISSED_RUNNERS],
        },
        "buy_gate_failures": {
            "total_events": gates.get("event_count", 0),
            "gate_summaries": gates.get("gate_summaries", []),
            "recent_events": gate_rows[:MAX_GATE_EVENTS],
            "truncated": gates.get("event_count", 0) > len(gate_rows[:MAX_GATE_EVENTS]),
        },
        "early_warning_cohorts": {
            "minimum_observations": cohorts.get("minimum_observations"),
            "cohorts": cohort_rows,
        },
        "early_warning_radar": {
            "candidate_count": radar.get("candidate_count", 0),
            "included_candidates_by_asset": {
                "stocks": sum(1 for row in radar_rows[:MAX_RADAR_CANDIDATES] if row.get("asset_type") == "stock"),
                "crypto": sum(1 for row in radar_rows[:MAX_RADAR_CANDIDATES] if row.get("asset_type") == "crypto"),
            },
            "candidates": radar_rows[:MAX_RADAR_CANDIDATES],
            "truncated": radar.get("candidate_count", 0) > len(radar_rows[:MAX_RADAR_CANDIDATES]),
        },
        "limits": [
            "A missed runner is a verified watchlist move, not proof a safe entry was available.",
            "BUY-gate follow-up returns are sampled observations, not intrabar paths or causal estimates.",
            "Only retained records and available official BUY history are included.",
            "Asset breakdowns describe included candidate samples, not full-universe totals.",
            "This export does not change scoring, eligibility, risk, paper trades, or live trading.",
        ],
    }
