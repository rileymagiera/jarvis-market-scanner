from datetime import datetime, timezone

import paper_trading
from experiment_registry import (
    ensure_registry, build_stop_decision_snapshot, record_stop_observation,
    summarize_experiment_registry,
)


def test_registry_is_versioned_and_stored_in_existing_account():
    account = {"cash": 250.0, "positions": [], "closed_trades": []}
    registry = ensure_registry(account)
    ids = {(x["experiment_id"], x["version"]) for x in registry["experiments"]}
    assert ("STOP-CURRENT-01", 1) in ids
    assert ("STOP-FIXED-025", 1) in ids
    assert ("STOP-ATR-175", 1) in ids
    assert ("EARLY-ENTRY-01", 1) in ids
    assert account["cash"] == 250.0
    assert "experiment_registry" in account


def test_stop_snapshot_preserves_exact_available_entry_calculation():
    candidate = {
        "price_source": "Kraken Spot public API",
        "price_as_of": "2026-11-19T15:00:00+00:00",
        "price_validation_passed": True,
        "market_regime": "NORMAL",
        "data_quality": {"data_type": "live", "age_seconds": 3, "session": "24/7 CRYPTO"},
        "levels": {"calculation": {
            "atr_14": 0.02, "risk_unit": 0.2, "support_reference": 9.8,
            "support_stop": 9.77, "stop_source": "nearby support",
        }},
        "relative_volume": 2.1, "change_1h_pct": 1.2,
    }
    snapshot = build_stop_decision_snapshot(candidate, 10.0, 9.77, "2026-11-19T15:00:00+00:00")
    assert snapshot["stop_calculation"]["atr_14"] == 0.02
    assert snapshot["stop_calculation"]["support_reference"] == 9.8
    assert snapshot["stop_calculation"]["stop_source"] == "nearby support"
    assert snapshot["indicators"]["relative_volume"] == 2.1
    assert snapshot["price_validation_passed"] is True


def test_counterfactual_stops_use_same_verified_sample_and_never_change_cash():
    account = {"cash": 175.0, "positions": [], "closed_trades": []}
    ensure_registry(account)
    position = {
        "entry_price": 10.0, "stop_loss": 9.7,
        "stop_decision_snapshot": {"stop_calculation": {"risk_unit": 0.2}},
        "stop_observations": [], "stop_counterfactuals": {},
    }
    cash_before = account["cash"]
    record_stop_observation(account, position, 9.72, "2026-11-19T15:05:00+00:00",
                            "2026-11-19T15:05:00+00:00", "scan-1")
    record_stop_observation(account, position, 9.72, "2026-11-19T15:05:00+00:00",
                            "2026-11-19T15:05:00+00:00", "scan-1")
    assert len(position["stop_observations"]) == 1
    assert position["stop_counterfactuals"]["STOP-FIXED-025"]["first_touch"] is not None
    assert position["stop_counterfactuals"]["STOP-FIXED-035"]["first_touch"] is None
    assert position["stop_counterfactuals"]["STOP-ATR-125"]["first_touch"] is not None
    assert account["cash"] == cash_before
    assert account["positions"] == []


def test_account_summary_exposes_registry_without_creating_second_account(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    account = paper_trading.load_account()
    summary = paper_trading.account_summary(account, datetime.now(timezone.utc))
    assert summary["experiment_registry"]["schema_version"] == 1
    assert len(list(tmp_path.iterdir())) == 0
    assert account["starting_cash"] == 250.0
    assert account["cash"] == 250.0


def test_verified_position_records_stop_inputs_and_counterfactuals(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    candidate = {
        "asset_type": "stock", "symbol": "TEST", "price": 10.0,
        "price_validation_passed": True, "price_as_of": "2026-11-19T15:00:00+00:00",
        "price_source": "test feed", "market_regime": "NORMAL",
        "data_quality": {"buy_eligible": True, "data_type": "live", "age_seconds": 5},
        "levels": {
            "watch_entry_low": 9.5, "watch_entry_high": 10.5,
            "stop_loss": 9.7, "target_1": 11.0, "target_2": 12.0,
            "calculation": {"atr_14": 0.2, "risk_unit": 0.2,
                            "support_reference": 9.8, "stop_source": "ATR stop"},
        },
    }
    signal = {
        "signal_id": "test-signal", "asset_type": "stock", "symbol": "TEST",
        "status": "active", "entry_low": 9.5, "entry_high": 10.5,
        "stop_loss": 9.7, "target_1": 11.0, "target_2": 12.0,
        "score": 90, "target_2_reward_risk_ratio": 2.0,
    }
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([candidate], [signal], now, scan_id="entry-scan")
    position = account["positions"][0]
    assert position["stop_decision_snapshot"]["stop_calculation"]["atr_14"] == 0.2
    assert position["stop_experiment_version"] == "STOP-CURRENT-01-v1"
    assert position["stop_observations"][0]["scan_id"] == "entry-scan"
    cash_after_entry = account["cash"]

    next_candidate = {**candidate, "price": 9.72}
    next_candidate["price_as_of"] = "2026-11-19T15:05:00+00:00"
    account = paper_trading.update_account([next_candidate], [], 
        datetime(2026, 11, 19, 15, 5, tzinfo=timezone.utc), scan_id="scan-2")
    assert account["positions"][0]["stop_counterfactuals"]["STOP-FIXED-025"]["first_touch"] is not None
    assert account["cash"] == cash_after_entry
