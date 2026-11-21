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
               "closed_trades": [{"realized_pnl": -3}], "ledger": [{"kind": "BUY"}],
               "secret": "dont-ever-include"}
    report = make_research_review(source, account)
    assert source == original
    assert report["coverage"]["retained_candidate_records"] == 780
    assert report["missed_runners"]["verified_watchlist_runners_without_official_buy"] == 30
    assert report["buy_gate_failures"]["top_blockers"][0]["measured"] == 0
    assert report["early_warning_setups"]["cohorts"][0]["sample_status"] == "early sample · do not promote"
    assert report["paper_trading"]["open_positions"] == 1
    assert report["paper_trading"]["closed_trades"] == 1
    assert report["paper_trading"]["event_count"] == 1
    assert report["paper_trading"]["closed_trade_outcomes"]["net_realized_pnl"] == -3
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



def test_replay_includes_real_nested_fee_aware_scenarios_and_crossing_denominators():
    export = _export()
    export["signal_lead_time"]["cohorts"] = [{
        "asset_type": "crypto", "signal": "Accumulation evidence developing",
        "threshold_pct": 20, "signals_observed": 30, "signals_with_followup": 27,
        "later_observed_crossings": 2, "already_crossed_at_first_signal": 20,
        "median_lead_minutes": 89.8, "sample_status": "directional review only",
    }]
    export["entry_timing_lab"]["fee_aware_replay"]["cohorts"] = [{
        "asset_type": "crypto", "research_phase": "EARLY WATCH",
        "horizon_minutes": 240, "missing_horizon_followup": 2,
        "sample_count": 7, "mean_gross_horizon_return_pct": 3.2,
        "observed_quote": {"net_wins": 3, "net_win_rate_pct": 42.86,
                           "mean_net_return_pct": 1.4},
        "adverse_exit": {"net_wins": 2, "net_win_rate_pct": 28.57,
                         "mean_net_return_pct": 0.9},
        "raw_private": "not for review",
    }]
    result = make_research_review(export, {})
    lead = result["early_signal_lead_time"]["cohorts"][0]
    assert lead["later_observed_crossings"] == 2
    assert lead["already_crossed_at_first_signal"] == 20
    assert lead["signals_with_followup"] == 27
    row = result["fee_aware_entry_replay"]["cohorts"][0]
    assert row["sample_count"] == 7
    assert row["observed_quote"]["mean_net_return_pct"] == 1.4
    assert row["adverse_exit"]["net_win_rate_pct"] == 28.57
    assert "raw_private" not in json.dumps(result)


def test_original_trade_ledger_intact_and_oversized_exit_flagged():
    from copy import deepcopy
    export = _export()
    export["missed_runner_case_studies"] = {"cases": [{
        "symbol": "ICNTUSDT", "asset_type": "crypto",
        "first_research_signal": {"at": "2026-11-20T21:01:00+00:00",
                                  "label": "EARLY ACCUMULATION"},
        "first_buy_gate_check": {"failed_gate_keys": ["market_not_risk_off"]},
        "secret": "not allowed",
    }]}
    trade = {
        "symbol": "QCADUSDT", "asset_type": "crypto", "strategy": "risk_off_experimental",
        "close_reason": "TARGET_2", "entry_price": 0.7588, "target_2": 0.842268,
        "close_price": 1.6644, "initial_quantity": 67.9815185999,
        "target_1_filled": False, "entry_fee": 0.25792188, "fees_paid": 0.82366408,
        "realized_pnl": 60.74039916, "price_source": "Kraken Spot public API",
        "closed_at": "2026-11-20T23:51:06+00:00",
    }
    account = {"ledger": [{"kind": "BUY"}, {"kind": "SELL"}],
               "closed_trades": [trade], "positions": []}
    original = deepcopy(account)
    result = make_research_review(export, account)
    assert account == original
    assert result["paper_trading"]["event_count"] == 2
    assert result["paper_trading"]["closed_trade_outcomes"]["net_realized_pnl"] == 60.7404
    assert result["missed_runners"]["case_studies"][0]["symbol"] == "ICNTUSDT"
    assert "not allowed" not in json.dumps(result)
    exec_watch = result["paper_execution_watch"]
    assert exec_watch["total_flagged_trades"] == 1
    row = exec_watch["flagged_trades"][0]
    assert row["symbol"] == "QCADUSDT"
    assert row["quote_overshoot_pct"] > 90
    assert 5 < row["target_2_capped_pnl_illustration"] < 5.3
    assert row["coinbase_trade_availability_at_exit"] == "not_independently_verified"
    assert row["order_book_fill_at_exit"] == "not_verified"


def test_runner_case_studies_use_existing_full_ledger_builder():
    from historical_audit_export import build_historical_audit_export
    record = {
        "asset_type": "crypto", "symbol": "ICNTUSDT",
        "discovery_max_gain_pct": 37.12, "discovery_return_status": "verified",
        "discovery_baseline_status": "live_verified",
        "discovery_initial_price": 0.50,
        "discovery_price_as_of": "2026-11-20T14:00:00+00:00",
        "first_seen": "2026-11-20T14:00:00+00:00",
        "signal_history": [], "buy_gate_history": [],
    }
    report = build_historical_audit_export([record], [])
    assert report["missed_runners"]["total_verified_watchlist_runners"] == 1
    assert report["missed_runner_case_studies"]["symbols"] == ["ICNTUSDT"]
    assert report["missed_runner_case_studies"]["cases"][0]["found"] is True
    assert report["missed_runner_case_studies"]["cases"][0]["discovery_first_accepted_price"] == 0.5
    digest = make_research_review(report, {})
    assert digest["missed_runners"]["case_studies"][0]["symbol"] == "ICNTUSDT"
