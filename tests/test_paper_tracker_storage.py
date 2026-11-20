import json
from datetime import datetime, timedelta, timezone

import send_alerts


def _candidate(now, verified=True):
    return {
        "asset_type": "crypto",
        "symbol": "TESTUSDT",
        "price": 100.0,
        "price_validation_passed": verified,
        "price_validation_status": "verified" if verified else "unverified",
        "price_validation_note": "test fixture",
        "price_as_of": now.isoformat(),
        "price_checked_at": now.isoformat(),
        "price_timestamp_type": "provider timestamp",
        "data_source": "test-feed",
        "stage": "EARLY WATCH",
        "score": 74,
        "catalyst_labels": ["test catalyst"],
        "early_runner_research_score": 81,
        "research_signal_matches": [{"feature": "test research"}],
        "early_runner_signal_matches": [{"feature": "test runner"}],
        "research_sample_high": 101.0,
        "research_sample_low": 99.0,
        "data_quality": {"data_type": "live", "age_seconds": 1, "session": "24/7"},
    }


def _use_temp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(send_alerts, "PAPER_STATE", tmp_path / "paper_tracker.json")
    monkeypatch.setattr(send_alerts, "PAPER_LIFETIME_STATE", tmp_path / "paper_tracker_lifetime.json")


def test_verified_tracker_keeps_frozen_features_without_duplicate_current_snapshot(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)

    tracker, _ = send_alerts.update_paper_tracker([_candidate(now)], now)
    record = tracker["crypto:TESTUSDT"]

    assert "research_feature_snapshot" not in record
    assert record["early_runner_initial_feature_snapshot"]["catalyst_labels"] == ["test catalyst"]
    assert record["early_runner_initial_feature_snapshot"]["early_runner_research_score"] == 81

    saved_text = (tmp_path / "paper_tracker.json").read_text(encoding="utf-8")
    assert "\n" not in saved_text.rstrip("\n")
    assert json.loads(saved_text)["crypto:TESTUSDT"]["early_runner_initial_feature_snapshot"] == record["early_runner_initial_feature_snapshot"]


def test_unverified_candidate_can_later_freeze_features_after_verification(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)

    first, _ = send_alerts.update_paper_tracker([_candidate(now, verified=False)], now)
    assert "research_feature_snapshot" not in first["crypto:TESTUSDT"]
    assert "early_runner_initial_feature_snapshot" not in first["crypto:TESTUSDT"]

    verified_at = now + timedelta(minutes=15)
    second, _ = send_alerts.update_paper_tracker([_candidate(verified_at, verified=True)], verified_at)
    record = second["crypto:TESTUSDT"]

    assert "research_feature_snapshot" not in record
    assert record["early_runner_initial_feature_snapshot"]["catalyst_labels"] == ["test catalyst"]
    assert record["early_runner_snapshot_at"] == verified_at.isoformat()


def test_existing_tracker_migrates_away_duplicate_snapshot_only(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    legacy_record = {
        "asset_type": "stock",
        "symbol": "LEGACY",
        "first_seen": now.isoformat(),
        "last_seen": now.isoformat(),
        "initial_price": 10.0,
        "price_validation_status": "verified",
        "research_feature_snapshot": {"catalyst_labels": ["redundant mutable copy"]},
        "early_runner_initial_feature_snapshot": {"catalyst_labels": ["frozen baseline"]},
    }
    (tmp_path / "paper_tracker.json").write_text(
        json.dumps({"stock:LEGACY": legacy_record}), encoding="utf-8"
    )

    tracker, _ = send_alerts.update_paper_tracker([], now)
    record = tracker["stock:LEGACY"]

    assert "research_feature_snapshot" not in record
    assert record["early_runner_initial_feature_snapshot"] == {"catalyst_labels": ["frozen baseline"]}


def test_delayed_verified_quote_repairs_stale_provisional_discovery_return(tmp_path, monkeypatch):
    """PYXS regression: display return follows the shown quote, not an old daily bar."""
    _use_temp_state(tmp_path, monkeypatch)
    now = datetime(2026, 11, 19, 18, 45, tzinfo=timezone.utc)
    first = _candidate(now)
    first.update({
        "asset_type": "stock", "symbol": "PYXS", "price": 1.485,
        "research_sample_high": 1.485, "research_sample_low": 1.485,
        "data_quality": {"data_type": "live", "age_seconds": 30,
                         "session": "REGULAR SESSION"},
    })
    first_result, _ = send_alerts.update_paper_tracker([first], now)
    original = first_result["stock:PYXS"]
    assert original["discovery_initial_price"] == 1.485
    original_observed_at = original["price_observed_at"]

    # Simulate the stale provisional value that survived into the saved ledger.
    original["discovery_return_pct"] = -5.05
    original["discovery_return_status"] = "provisional"
    original["discovery_return_price_as_of"] = "2026-11-18T05:00:00+00:00"
    send_alerts.PAPER_STATE.write_text(
        json.dumps(first_result), encoding="utf-8"
    )

    later = now + timedelta(hours=18, minutes=15)
    cross_checked_delayed = {**first,
        "price": 1.875,
        "price_validation_passed": False,
        "price_validation_status": "verified",
        "price_as_of": later.isoformat(),
        "price_checked_at": later.isoformat(),
        "data_quality": {"data_type": "delayed", "age_seconds": 47,
                         "session": "REGULAR SESSION"},
    }
    updated, _ = send_alerts.update_paper_tracker([cross_checked_delayed], later)
    record = updated["stock:PYXS"]
    assert record["latest_price"] == 1.875
    assert record["discovery_initial_price"] == 1.485
    assert record["discovery_return_pct"] == 26.26
    assert record["discovery_return_status"] == "provisional"
    assert record["discovery_return_price_as_of"] == later.isoformat()
    # Delayed quotes must NOT advance live research/official outcome clocks.
    assert record["price_observed_at"] == original_observed_at
    assert record["return_pct"] == 0.0
