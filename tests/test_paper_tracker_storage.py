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
