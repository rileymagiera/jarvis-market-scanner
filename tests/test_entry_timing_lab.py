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
        snap(120, 15),
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
