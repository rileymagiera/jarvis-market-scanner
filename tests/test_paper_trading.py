import json
from datetime import datetime, timezone

import paper_trading


def _signal():
    return {
        "signal_id": "stock:TEST:2026-11-19T00:00:00+00:00",
        "asset_type": "stock",
        "symbol": "TEST",
        "status": "active",
        "entry_low": 9.5,
        "entry_high": 10.5,
        "stop_loss": 9.0,
        "target_1": 11.0,
        "target_2": 12.0,
        "score": 90,
        "score_model": "hybrid_offline_v2",
    }


def _candidate(price, verified=True):
    return {
        "asset_type": "stock",
        "symbol": "TEST",
        "price": price,
        "price_validation_passed": verified,
        "price_as_of": "2026-11-19T00:00:00+00:00",
        "price_source": "test feed",
    }


def test_verified_buy_signal_opens_sized_paper_position(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    account = paper_trading.update_account(
        [_candidate(10.0)], [_signal()], datetime.now(timezone.utc)
    )
    assert account["cash"] < 250
    assert len(account["positions"]) == 1
    position = account["positions"][0]
    assert position["entry_price"] == 10.0
    assert position["notional"] <= 62.5
    assert position["quantity"] > 0
    assert account["ledger"][0]["type"] == "BUY"
    summary = paper_trading.account_summary(account)
    assert summary["starting_cash"] == 250
    assert summary["equity"] <= 250


def test_unverified_price_never_opens_a_paper_position(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    account = paper_trading.update_account(
        [_candidate(10.0, verified=False)], [_signal()], datetime.now(timezone.utc)
    )
    assert account["cash"] == 250
    assert account["positions"] == []
    assert account["ledger"] == []


def test_stop_observation_closes_position_without_reentry(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime.now(timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], now)
    account = paper_trading.update_account([_candidate(8.9)], [_signal()], now)
    assert account["positions"] == []
    assert len(account["closed_trades"]) == 1
    assert account["closed_trades"][0]["close_reason"] == "STOP"
    assert account["cash"] < 250
    saved = json.loads((tmp_path / "paper.json").read_text())
    assert saved["processed_signal_ids"] == [_signal()["signal_id"]]
