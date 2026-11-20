"""Regression tests for no-lookahead, verified, read-only entry timing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from entry_timing_lab import build_entry_timing_lab


BASE = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)


def snap(index, price, phase="EARLY WATCH", *, verified=True, fresh=True,
         quote_offset=0, status="verified"):
    observed = BASE + timedelta(minutes=index)
    quoted = observed + timedelta(minutes=quote_offset)
    return {
        "scan_id": f"scan-{index}",
        "observed_at": observed.isoformat(),
        "price_as_of": quoted.isoformat(),
        "price_verified": verified,
        "price_validation_status": status,
        "outcome_eligible": fresh,
        "price": price,
        "shadow_classification": phase,
    }


def test_uses_first_newer_next_scan_as_shadow_entry_and_forward_only_returns():
    record = {"asset_type": "crypto", "symbol": "ABCUSDT", "signal_history": [
        snap(0, 10), snap(15, 11, "BREAKOUT DEVELOPING"),
        snap(75, 12.1, "BREAKOUT DEVELOPING"),
        snap(255, 9.9, "BREAKOUT DEVELOPING"),
    ]}
    original = deepcopy(record)
    result = build_entry_timing_lab([record])
    assert record == original
    assert result["mode"] == "read_only_shadow_analysis"
    assert result["eligible_phase_events"] == 2
    assert result["events_without_next_fresh_scan"] == 1  # breakout next scan gap >30min
    rows = [x for x in result["cohorts"] if x["research_phase"] == "EARLY WATCH"]
    assert len(rows) == 2
    assert rows[0]["horizon_minutes"] == 60
    assert rows[0]["median_gross_return_pct"] == 10.0
    assert rows[0]["positive_gross_followup_pct"] == 100.0
    assert rows[1]["horizon_minutes"] == 240
    assert rows[1]["median_gross_return_pct"] == -10.0
    assert result["examples"][0]["next_scan_price"] == 11.0
    assert all(x["review_status"] == "insufficient_sample_do_not_rank" for x in rows)


def test_repeated_quote_cannot_be_used_as_next_scan_entry():
    record = {"asset_type": "stock", "symbol": "AAA", "signal_history": [
        snap(0, 10),
        snap(15, 10.2, quote_offset=-15),
        snap(75, 10.8),
    ]}
    result = build_entry_timing_lab([record])
    assert result["eligible_phase_events"] == 1
    assert result["events_without_next_fresh_scan"] == 1
    assert result["cohorts"] == []


def test_explicit_stale_or_unverified_snapshots_never_count():
    record = {"asset_type": "stock", "symbol": "BBB", "signal_history": [
        snap(0, 10, status="stale"),
        snap(15, 11, verified=False),
        snap(30, 12, fresh=False),
        snap(45, 13),
        snap(60, 14),
        snap(105, 15),
    ]}
    report = build_entry_timing_lab([record])
    assert report["excluded_invalid_snapshots"] == 3
    assert report["eligible_candidates"] == 1
    assert report["eligible_phase_events"] == 1
    assert report["cohorts"] == []  # no full +60 after next entry


def test_outcome_observation_late_by_more_than_30_minutes_is_unknown():
    record = {"asset_type": "crypto", "symbol": "LATE", "signal_history": [
        snap(0, 10), snap(15, 10.5), snap(115, 50),
    ]}
    result = build_entry_timing_lab([record])
    assert result["eligible_phase_events"] == 1
    assert result["events_without_next_fresh_scan"] == 0
    assert result["cohorts"] == []  # 100-minute delay relative to entry: not +60 within tolerance


def test_next_observation_missing_is_not_scored_as_loss():
    record = {"asset_type": "crypto", "symbol": "WAIT", "signal_history": [snap(0, 3)]}
    result = build_entry_timing_lab([record])
    assert result["eligible_phase_events"] == 1
    assert result["events_without_next_fresh_scan"] == 1
    assert result["cohorts"] == []


def test_independent_assets_and_first_phase_once_per_candidate():
    rows = [
        {"asset_type": "crypto", "symbol": "A", "signal_history": [
            snap(0, 1, phase="PRE-BREAKOUT WATCH"),
            snap(15, 2, phase="PRE-BREAKOUT WATCH"),
            snap(75, 4, phase="PRE-BREAKOUT WATCH"),
        ]},
        {"asset_type": "stock", "symbol": "B", "signal_history": [
            snap(0, 1, phase="EARLY ACCUMULATION"),
            snap(15, 2, phase="EARLY ACCUMULATION"),
            snap(75, 3, phase="EARLY ACCUMULATION"),
        ]},
    ]
    result = build_entry_timing_lab(rows)
    assert len(result["cohorts"]) == 2
    assert result["next_scan_samples_by_asset"] == {"stock": 1, "crypto": 1}
    assert result["eligible_phase_events"] == 2
    assert {row["research_phase"] for row in result["cohorts"]} == {
        "PRE-BREAKOUT WATCH", "EARLY ACCUMULATION",
    }


def test_bad_input_empty_report():
    result = build_entry_timing_lab([None, {"asset_type": "nonsense"}, {}])
    assert result["eligible_candidates"] == 0
    assert result["cohorts"] == []
    assert not any("BUY NOW" in x for x in result["limitations"])


def test_stop_first_then_rally_is_sampled_sequence_only_and_keeps_input_unchanged():
    record = {"asset_type": "crypto", "symbol": "RUNUSDT", "signal_history": [
        snap(0, 10), snap(15, 10),
        snap(30, 9.6), snap(45, 11), snap(75, 10.8),
    ]}
    original = deepcopy(record)
    report = build_entry_timing_lab([record])
    assert record == original
    cohort = next(x for x in report["cohorts"] if x["horizon_minutes"] == 60)
    assert cohort["sampled_candidates"] == 1
    assert cohort["sampled_stop_before_rally_count"] == 1
    assert cohort["sampled_rally_before_stop_count"] == 0
    assert cohort["median_sampled_stop_to_rally_min"] == 15
    assert report["sampled_path_review_thresholds_pct"] == {"stop": -3.0, "rally": 8.0}
    assert report["sampled_stop_before_rally_examples"][0]["symbol"] == "RUNUSDT"
    assert report["mode"] == "read_only_shadow_analysis"


def test_rally_first_then_stop_is_not_marked_stop_before_rally():
    record = {"asset_type": "stock", "symbol": "REV", "signal_history": [
        snap(0, 10), snap(15, 10), snap(30, 11), snap(45, 9.5), snap(75, 9.8),
    ]}
    report = build_entry_timing_lab([record])
    cohort = next(x for x in report["cohorts"] if x["horizon_minutes"] == 60)
    assert cohort["sampled_stop_before_rally_count"] == 0
    assert cohort["sampled_rally_before_stop_count"] == 1
    assert cohort["median_sampled_stop_to_rally_min"] is None


def test_repeated_quote_does_not_create_fake_stop_before_rally():
    record = {"asset_type": "crypto", "symbol": "REPEAT", "signal_history": [
        snap(0, 10), snap(15, 10),
        snap(30, 9.5, quote_offset=-15),  # recycled entry quote, not new price
        snap(45, 11), snap(75, 10.7),
    ]}
    report = build_entry_timing_lab([record])
    cohort = next(x for x in report["cohorts"] if x["horizon_minutes"] == 60)
    assert cohort["sampled_stop_before_rally_count"] == 0
    assert cohort["sampled_rally_only_count"] == 1


def test_no_full_followup_is_unknown_and_not_a_stop_only_loss():
    record = {"asset_type": "crypto", "symbol": "GAP", "signal_history": [
        snap(0, 10), snap(15, 10), snap(30, 9), snap(45, 12),
    ]}
    report = build_entry_timing_lab([record])
    assert report["cohorts"] == []
    assert report["sampled_stop_before_rally_examples"] == []


def test_path_outcome_count_is_exhaustive_for_sampled_cohort():
    records = [
        {"asset_type": "crypto", "symbol": "STP", "signal_history": [
            snap(0, 10), snap(15, 10), snap(30, 9.5), snap(75, 9.8)]},
        {"asset_type": "crypto", "symbol": "NTH", "signal_history": [
            snap(0, 10), snap(15, 10), snap(30, 10.1), snap(75, 10.2)]},
    ]
    report = build_entry_timing_lab(records)
    cohort = next(x for x in report["cohorts"] if x["horizon_minutes"] == 60)
    assert cohort["sampled_candidates"] == 2
    counts = [cohort[key] for key in (
        "sampled_stop_before_rally_count", "sampled_rally_before_stop_count",
        "sampled_stop_only_count", "sampled_rally_only_count",
        "neither_threshold_observed_count")]
    assert counts == [0, 0, 1, 0, 1]
    assert sum(counts) == cohort["sampled_candidates"]
