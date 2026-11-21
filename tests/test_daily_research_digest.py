"""The daily research review never affects execution or includes raw history."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from daily_research_digest import make_research_review, persist_review, update_daily_reviews


def _export(at="2026-11-21T03:00:00+00:00"):
    return {
        "export_type": "jarvis_historical_opportunity_audit", "generated_at": at,
        "provenance": {"scan_run_id": "1234", "last_scan_completed_at": at,
                       "retained_candidate_records": 780, "official_buy_signal_records": 17},
        "missed_runners": {"total_verified_watchlist_runners": 30, "threshold_pct": 20,
                           "candidates": [{"symbol": "A", "asset_type": "stock", "discovery_max_gain_pct": 30,
                                           "raw_history": [1, 2, 3]}] * 200},
        "buy_gate_failures": {"total_events": 410, "gate_summaries": [{"gate": "price_unverified",
                                 "events": 170, "measured": 0, "unknown_big_field": "x" * 10000}]},
        "signal_lead_time": {"eligible_snapshots": 100, "excluded_snapshots": 21,
                             "cohorts": [{"signal": "rvol", "signals_with_followup": 24,
                                          "median_lead_minutes": 17}]},
        "entry_timing_lab": {"eligible_candidates": 52, "events_without_next_fresh_scan": 12,
                             "fee_aware_replay": {"cohorts": [{"asset_type": "stock",
                             "horizon_minutes": 240, "sample_status": "insufficient_sample"}]}},
        "gate_combination_shadow": {"minimum_unique_followups_for_directional_label": 8,
                                    "profiles": [{"name": "without_volume_gate",
                                      "newly_qualifying_unique_candidates": 9,
                                      "sample_status": "directional_only_not_executable"}]},
        "early_warning_cohorts": {"minimum_observations": 8,
                                  "cohorts": [{"asset_type": "crypto", "setup": "vwap_reclaim",
                                               "observations": 5, "runner_rate_pct": 40,
                                               "sample_status": "early sample · do not promote"}]},
    }


def test_review_preserves_full_source_and_excludes_raw_history():
    source = _export()
    original = copy.deepcopy(source)
    account = {"starting_cash": 250, "positions": [{"symbol": "BTC"}],
               "closed_trades": [{"pnl": -3}], "events": [{"kind": "BUY"}],
               "secret": "dont-ever-include"}
    report = make_research_review(source, account)
    assert source == original
    assert report["coverage"]["retained_candidate_records"] == 780
    assert report["missed_runners"]["verified_watchlist_runners_without_official_buy"] == 30
    assert report["buy_gate_failures"]["top_blockers"][0]["measured"] == 0
    assert report["early_warning_setups"]["cohorts"][0]["sample_status"] == "early sample · do not promote"
    assert report["paper_trading"]["open_positions"] == 1
    assert report["paper_trading"]["closed_trades"] == 1
    assert "dont-ever-include" not in json.dumps(report)
    assert "raw_history" not in json.dumps(report)
    assert len(report["missed_runners"]["examples"]) <= 8
    assert len(json.dumps(report)) < 6000
    assert report["read_only"] is True


def test_daily_rollover_keeps_last_fourteen_unique_dates():
    previous = {}
    day = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for i in range(19):
        review = make_research_review(_export((day + timedelta(days=i)).isoformat()))
        previous = update_daily_reviews(previous, review)
        assert len(previous["daily"]) <= 14
    assert len(previous["daily"]) == 14
    assert previous["latest"]["date_utc"] == "2026-11-19"
    assert previous["daily"][-1]["date_utc"] == "2026-11-06"
    rerun = make_research_review(_export("2026-11-19T18:05:00+00:00"))
    previous = update_daily_reviews(previous, rerun)
    assert len(previous["daily"]) == 14
    assert previous["latest"]["generated_at"] == "2026-11-19T18:05:00+00:00"
    assert previous["daily"][0]["scan_run_id"] == "1234"


def test_persist_round_trip_and_reject_invalid_audit(tmp_path):
    audit = tmp_path / "audit.json"
    account = tmp_path / "paper.json"
    output = tmp_path / "review.json"
    audit.write_text(json.dumps(_export()), encoding="utf-8")
    account.write_text(json.dumps({"positions": [], "events": []}), encoding="utf-8")
    first = persist_review(audit, account, output)
    again = persist_review(audit, account, output)
    assert first == again
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1
    audit.write_text(json.dumps({"generated_at": "2026-11-21T03:00:00+00:00"}), encoding="utf-8")
    with pytest.raises(ValueError, match="complete GitHub Actions historical audit"):
        persist_review(audit, account, output)


def test_existing_scan_workflow_persists_digest_without_new_scan_job():
    workflow = Path(".github/workflows/jarvis-alerts.yml").read_text(encoding="utf-8")
    render = Path("render.yaml").read_text(encoding="utf-8")
    assert "name: Publish daily full-history research review" in workflow
    assert "python daily_research_digest.py" in workflow
    assert "git add -f jarvis_research_review.json" in workflow
    assert "jarvis_research_review.json" in render
    assert "name: Build compact historical opportunity audit export" in workflow
