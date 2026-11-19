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


def test_coinbase_one_app_fee_model_uses_zero_commission_and_estimated_spread():
    buy = paper_trading.calculate_trade_fee("crypto", "buy", 100.0)
    sell = paper_trading.calculate_trade_fee("crypto", "sell", 100.0)
    assert buy["broker"] == "Coinbase app + Coinbase One Basic"
    assert buy["commission"] == 0
    assert buy["total"] == 0.5
    assert sell["total"] == 0.5
    assert buy["monthly_zero_fee_cap"] == 500.0
    assert "Estimated spread" in buy["note"]

def test_webull_stock_fees_apply_cat_both_sides_and_sec_only_on_sell():
    buy = paper_trading.calculate_trade_fee("stock", "buy", 1000.0, quantity=10)
    sell = paper_trading.calculate_trade_fee("stock", "sell", 1000.0, quantity=10)
    assert buy["broker"] == "Webull"
    assert buy["commission"] == 0
    assert buy["cat_fee"] == 0.003
    assert buy["sec_fee"] == 0
    assert sell["cat_fee"] == 0.003
    assert sell["sec_fee"] == 0.0206
    assert sell["total"] == 0.0236


def test_paper_entry_and_exit_ledger_use_asset_specific_fees(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    signal = _signal()
    candidate = _candidate(10.0)
    candidate["asset_type"] = "crypto"
    signal["asset_type"] = "crypto"
    now = datetime.now(timezone.utc)
    account = paper_trading.update_account([candidate], [signal], now)
    position = account["positions"][0]
    assert position["entry_fee"] > 0
    assert position["entry_fee_breakdown"]["broker"] == "Coinbase app + Coinbase One Basic"
    candidate["price"] = 11.0
    account = paper_trading.update_account([candidate], [signal], now)
    sell = [x for x in account["ledger"] if x["type"] == "SELL"][0]
    assert sell["fee_breakdown"]["broker"] == "Coinbase app + Coinbase One Basic"
    assert sell["fee"] > 0
