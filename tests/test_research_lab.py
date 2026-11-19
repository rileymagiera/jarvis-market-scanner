import json
from datetime import datetime, timedelta, timezone

import pytest

import research_lab


NOW = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)


def _candidate(price=10.0, verified=True, as_of=None):
    return {
        "asset_type": "crypto",
        "symbol": "LABCOIN",
        "price": price,
        "price_validation_passed": verified,
        "price_as_of": (as_of or NOW).isoformat(),
        "price_source": "Kraken Spot",
    }


def _signal(**overrides):
    signal = {
        "signal_id": "lab:crypto:LABCOIN:exp-v1:001",
        "asset_type": "crypto",
        "symbol": "LABCOIN",
        "experimental": True,
        "status": "active",
        "strategy_id": "early_entry",
        "strategy_version": "1",
        "entry_low": 9.5,
        "entry_high": 10.5,
        "stop_loss": 9.0,
        "target_1_price": 11.0,
        "target_1_fraction": 0.5,
        "target_price": 12.0,
        "lab_notional": 100.0,
        "fee_rate": 0.01,
        "slippage_rate": 0.01,
        "market_regime": "NEUTRAL",
        "feature_snapshot": {"relative_volume": 2.1},
    }
    signal.update(overrides)
    return signal


def _enabled_account(path):
    account = research_lab.load_account(path)
    account["enabled"] = True
    research_lab.save_account(account, path)
    return account


def test_default_account_is_separate_disabled_250_paper_only(tmp_path):
    lab = research_lab.load_account(tmp_path / "lab.json")
    assert lab["account"] == "research_lab"
    assert lab["mode"] == "PAPER_ONLY"
    assert lab["enabled"] is False
    assert lab["starting_cash"] == 250.0
    assert lab["cash"] == 250.0
    assert lab["positions"] == []


def test_disabled_lab_does_not_write_or_enter(tmp_path):
    path = tmp_path / "lab.json"
    before = research_lab.load_account(path)
    result = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path)
    assert result == before
    assert not path.exists()


def test_explicitly_enabled_lab_opens_only_tagged_experimental_signal(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    signals = [_signal(), _signal(signal_id="official-buy", experimental=False)]
    result = research_lab.process_scan([_candidate()], signals, NOW, "run-1", path, enabled=True)
    assert len(result["positions"]) == 1
    position = result["positions"][0]
    assert position["signal_id"] == "lab:crypto:LABCOIN:exp-v1:001"
    assert position["experimental"] is True
    assert position["strategy_id"] == "early_entry"
    assert result["cash"] < 250.0
    assert all(row.get("signal_id") != "official-buy" for row in result["ledger"])


def test_scan_and_signal_processing_are_idempotent(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    first = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    second = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    assert len(first["positions"]) == len(second["positions"]) == 1
    third = research_lab.process_scan([_candidate()], [_signal()], NOW + timedelta(minutes=1), "run-2", path, enabled=True)
    assert len(third["positions"]) == 1
    assert len([x for x in third["ledger"] if x["type"] == "BUY"]) == 1


def test_unverified_or_stale_prices_never_open_or_close_positions(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    stale = _candidate(as_of=NOW - timedelta(hours=1))
    opened = research_lab.process_scan([stale], [_signal()], NOW, "run-1", path, enabled=True)
    assert opened["positions"] == []
    verified = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-2", path, enabled=True)
    assert len(verified["positions"]) == 1
    stale_exit = _candidate(price=8.0, as_of=NOW - timedelta(hours=1))
    unchanged = research_lab.process_scan([stale_exit], [], NOW + timedelta(minutes=2), "run-3", path, enabled=True)
    assert len(unchanged["positions"]) == 1


def test_missing_cost_assumptions_or_invalid_levels_do_not_enter(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    no_fee = _signal(signal_id="no-fee", fee_rate=None)
    bad_stop = _signal(signal_id="bad-stop", stop_loss=10.5)
    result = research_lab.process_scan([_candidate()], [no_fee, bad_stop], NOW, "run-1", path, enabled=True)
    assert result["positions"] == []
    assert result["cash"] == 250.0


def test_insufficient_cash_is_rejected_without_negative_balance(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    result = research_lab.process_scan(
        [_candidate()], [_signal(lab_notional=250.0)], NOW, "run-1", path, enabled=True
    )
    assert result["positions"] == []
    assert result["cash"] == 250.0
    assert any(row["type"] == "REJECT" for row in result["ledger"])


def test_fee_and_slippage_are_recorded_in_entry_accounting(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    result = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    position = result["positions"][0]
    assert position["entry_price"] == pytest.approx(10.1)
    assert position["entry_fee"] == pytest.approx(1.0)
    assert result["cash"] == pytest.approx(149.0)


def test_target_one_partial_exit_then_target_closes_remainder(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    opened = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    original_quantity = opened["positions"][0]["quantity"]
    partial = research_lab.process_scan(
        [_candidate(price=11.1)], [], NOW + timedelta(minutes=1), "run-2", path, enabled=True
    )
    assert partial["positions"][0]["quantity"] == pytest.approx(original_quantity * 0.5)
    assert partial["positions"][0]["target_1_filled"] is True
    closed = research_lab.process_scan(
        [_candidate(price=12.1)], [], NOW + timedelta(minutes=2), "run-3", path, enabled=True
    )
    assert closed["positions"] == []
    assert len(closed["closed_trades"]) == 1
    assert closed["closed_trades"][0]["close_reason"] == "TARGET"
    assert closed["cash"] > 149.0


def test_stop_exit_closes_full_position(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    stopped = research_lab.process_scan(
        [_candidate(price=8.9)], [], NOW + timedelta(minutes=1), "run-2", path, enabled=True
    )
    assert stopped["positions"] == []
    assert stopped["closed_trades"][0]["close_reason"] == "STOP"
    assert stopped["cash"] < 250.0


def test_lab_never_mutates_core_state_file(tmp_path):
    core_path = tmp_path / "paper_trading_state.json"
    core_bytes = b'{"version":1,"cash":245.27,"positions":[],"sentinel":"core"}\n'
    core_path.write_bytes(core_bytes)
    lab_path = tmp_path / "research_lab_state.json"
    _enabled_account(lab_path)
    research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", lab_path, enabled=True)
    assert core_path.read_bytes() == core_bytes
    assert lab_path.exists()


def test_corrupt_or_wrong_identity_state_fails_closed(tmp_path):
    path = tmp_path / "lab.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        research_lab.load_account(path)
    path.write_text(json.dumps({"schema_version": 1, "mode": "PAPER_ONLY", "account": "core"}), encoding="utf-8")
    with pytest.raises(ValueError):
        research_lab.load_account(path)


def test_summary_reports_equity_and_drawdown(tmp_path):
    path = tmp_path / "lab.json"
    _enabled_account(path)
    result = research_lab.process_scan([_candidate()], [_signal()], NOW, "run-1", path, enabled=True)
    summary = research_lab.account_summary(result)
    assert summary["account"] == "research_lab"
    assert summary["mode"] == "PAPER_ONLY"
    assert summary["open_positions"] == 1
    assert summary["equity"] > 0
    assert summary["cash"] < 250.0
