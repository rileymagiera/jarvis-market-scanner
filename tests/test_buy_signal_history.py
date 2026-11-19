import os
from datetime import datetime, timedelta, timezone

# send_alerts reads these values at import time; tests never send mail.
os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import send_alerts


def _candidate(stage="BUY", price=10.0, verified=True):
    return {
        "asset_type": "stock",
        "symbol": "TEST",
        "stage": stage,
        "price": price,
        "price_validation_passed": verified,
        "data_source": "test-feed",
        "price_as_of": "2026-11-19T14:30:00+00:00",
        "score": 88,
        "levels": {
            "watch_entry_low": 9.5,
            "watch_entry_high": 10.5,
            "invalidation_level": 9.0,
            "stop_loss": 9.0,
            "target_1": 11.0,
            "target_2": 12.0,
        },
    }


def _start(tmp_path, monkeypatch, now):
    monkeypatch.setattr(send_alerts, "BUY_SIGNAL_STATE", tmp_path / "buy_signals.json")
    return send_alerts.update_buy_signal_history([_candidate()], now)


def test_repeated_buy_scans_update_one_active_signal(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    first = _start(tmp_path, monkeypatch, now)
    first_id = first[0]["signal_id"]

    second = send_alerts.update_buy_signal_history(
        [_candidate(price=10.2)], now + timedelta(minutes=15)
    )

    assert len(second) == 1
    assert second[0]["signal_id"] == first_id
    assert second[0]["status"] == "active"
    assert second[0]["latest_return_pct"] == 2.0


def test_verified_non_buy_scan_closes_signal_and_keeps_forward_outcomes(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    _start(tmp_path, monkeypatch, now)

    result = send_alerts.update_buy_signal_history(
        [_candidate(stage="EARLY SETUP", price=10.8)],
        now + timedelta(hours=2),
    )

    signal = result[0]
    assert signal["status"] == "closed"
    assert signal["closed_at"] == (now + timedelta(hours=2)).isoformat()
    assert signal["outcomes"]["1h"]["return_pct"] == 8.0


def test_missing_signal_becomes_stale_after_six_clean_scans(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    _start(tmp_path, monkeypatch, now)

    for index in range(1, 7):
        result = send_alerts.update_buy_signal_history(
            [], now + timedelta(minutes=15 * index), scan_errors=[]
        )

    assert result[0]["status"] == "stale"
    assert result[0]["clean_scans_missing"] == 6
    assert result[0]["closure_reason"] == "Absent from six consecutive completed scans"


def test_scan_errors_pause_missing_signal_stale_counter(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    _start(tmp_path, monkeypatch, now)

    for index in range(1, 8):
        result = send_alerts.update_buy_signal_history(
            [], now + timedelta(minutes=15 * index), scan_errors=["provider timeout"]
        )

    assert result[0]["status"] == "active"
    assert result[0]["clean_scans_missing"] == 0


def test_unverified_non_buy_price_does_not_close_active_signal(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    _start(tmp_path, monkeypatch, now)

    result = send_alerts.update_buy_signal_history(
        [_candidate(stage="WATCH", price=9.8, verified=False)],
        now + timedelta(minutes=15),
    )

    assert result[0]["status"] == "active"
    assert result[0]["clean_scans_missing"] == 0


def test_target_crossings_are_recorded_as_observations_not_fills(tmp_path, monkeypatch):
    now = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
    _start(tmp_path, monkeypatch, now)

    result = send_alerts.update_buy_signal_history(
        [_candidate(price=11.2)], now + timedelta(hours=1)
    )

    observations = result[0]["level_observations"]
    assert observations["target_1_observed"]["observed_price"] == 11.2
    assert "target_2_observed" not in observations


def test_history_learning_excludes_non_live_stock_outcomes():
    tracker = {
        f"stock:OLD{i}": {
            "asset_type": "stock",
            "price_validation_status": "verified",
            "data_quality": {"data_type": "eod"},
            "early_runner_initial_feature_snapshot": {"catalyst_labels": ["FDA approval"]},
            "early_runner_outcomes": {"5d": {"return_pct": 30.0}},
        }
        for i in range(10)
    }
    candidate = {
        "asset_type": "stock",
        "catalyst_labels": ["FDA approval"],
        "early_runner_research_score": 80,
    }

    result = send_alerts.apply_history_runner_learning([candidate], tracker)

    assert result[0]["history_runner_learning"]["score_bonus"] == 0
    assert result[0]["history_runner_learning"]["matched_features"] == []


def test_history_learning_requires_eight_verified_five_day_stock_outcomes():
    tracker = {}
    for i in range(8):
        tracker[f"stock:LIVE{i}"] = {
            "asset_type": "stock",
            "price_validation_status": "verified",
            "data_quality": {"data_type": "live"},
            "early_runner_initial_feature_snapshot": {"catalyst_labels": ["FDA approval"]},
            "early_runner_outcomes": {"5d": {"return_pct": 25.0}},
        }
    candidate = {
        "asset_type": "stock",
        "catalyst_labels": ["FDA approval"],
        "early_runner_research_score": 80,
    }

    result = send_alerts.apply_history_runner_learning([candidate], tracker)

    assert result[0]["history_runner_learning"]["score_bonus"] == 10
    catalyst = [
        item for item in result[0]["history_runner_learning"]["matched_features"]
        if item["feature"] == "catalyst_present"
    ]
    assert len(catalyst) == 1
    assert catalyst[0]["n"] == 8
