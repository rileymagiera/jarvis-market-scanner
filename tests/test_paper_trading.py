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


def test_coinbase_one_monthly_volume_tracks_buys_sells_and_allowance_overrun():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    account = {"coinbase_one_monthly_volume": {}}
    first = paper_trading._record_coinbase_monthly_volume(account, 320.0, now)
    second = paper_trading._record_coinbase_monthly_volume(account, 220.0, now)
    assert first["eligible_volume"] == 320.0
    assert first["remaining_allowance"] == 180.0
    assert second["eligible_volume"] == 180.0
    assert second["volume_over_cap"] == 40.0
    assert second["monthly_volume_after"] == 540.0
    assert second["standard_commission_status"] == "rate_not_configured"


def test_coinbase_one_monthly_volume_rolls_into_new_calendar_month():
    account = {"coinbase_one_monthly_volume": {"2026-11": 500.0}}
    december = datetime(2026, 12, 1, tzinfo=timezone.utc)
    result = paper_trading._record_coinbase_monthly_volume(account, 25.0, december)
    assert result["month"] == "2026-12"
    assert result["eligible_volume"] == 25.0
    assert result["remaining_allowance"] == 475.0


def test_position_sizing_keeps_stop_loss_risk_within_one_percent_after_fees(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    account = paper_trading.update_account(
        [_candidate(10.0)], [_signal()], datetime.now(timezone.utc)
    )
    position = account["positions"][0]
    quantity = position["quantity"]
    entry_fee = position["entry_fee"]
    stop_exit_fee = paper_trading.calculate_trade_fee(
        "stock", "sell", 9.0 * quantity, quantity
    )["total"]
    modeled_stop_loss = quantity * (10.0 - 9.0) + entry_fee + stop_exit_fee
    assert modeled_stop_loss <= 2.5


def test_repeated_scan_does_not_duplicate_paper_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    first = paper_trading.update_account([_candidate(10.0)], [_signal()], now)
    first_cash = first["cash"]
    second = paper_trading.update_account([_candidate(10.1)], [_signal()], now)

    assert len(second["positions"]) == 1
    assert second["cash"] == first_cash
    assert len([event for event in second["ledger"] if event["type"] == "BUY"]) == 1


def test_target_one_is_partial_once_then_target_two_closes_remainder(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], now)
    initial_qty = account["positions"][0]["quantity"]

    account = paper_trading.update_account([_candidate(11.0)], [_signal()], now)
    assert len(account["positions"]) == 1
    assert account["positions"][0]["quantity"] == round(initial_qty * 0.5, 10)
    assert account["positions"][0]["target_1_filled"] is True

    account = paper_trading.update_account([_candidate(11.0)], [_signal()], now)
    assert len([event for event in account["ledger"] if event["type"] == "SELL"]) == 1

    account = paper_trading.update_account([_candidate(12.0)], [_signal()], now)
    assert account["positions"] == []
    assert len(account["closed_trades"]) == 1
    sells = [event for event in account["ledger"] if event["type"] == "SELL"]
    assert [event["message"] for event in sells] == [
        "TARGET_1 observed for TEST", "TARGET_2 observed for TEST"
    ]
    assert account["closed_trades"][0]["quantity"] == 0.0
    summary = paper_trading.account_summary(account)
    assert summary["open_positions"] == 0
    assert summary["closed_trades"] == 1
    assert summary["equity"] == round(account["cash"], 2)


def test_unverified_followup_cannot_mark_open_position_to_market_or_trigger_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], now)
    cash_after_entry = account["cash"]

    account = paper_trading.update_account(
        [_candidate(8.0, verified=False)], [_signal()], now
    )

    assert len(account["positions"]) == 1
    assert account["positions"][0]["latest_price"] == 10.0
    assert account["cash"] == cash_after_entry
    assert not [event for event in account["ledger"] if event["type"] == "SELL"]
    assert account["positions"][0]["price_update_status"] == "latest_scan_price_unverified"
    summary = paper_trading.account_summary(account)
    assert summary["equity"] == round(cash_after_entry + account["positions"][0]["quantity"] * 10.0, 2)


def test_missing_position_price_is_flagged_stale_without_exiting(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    first_scan = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], first_scan)
    original_price = account["positions"][0]["latest_price"]

    next_scan = first_scan.replace(minute=31)
    account = paper_trading.update_account([], [], next_scan)

    assert len(account["positions"]) == 1
    position = account["positions"][0]
    assert position["latest_price"] == original_price
    assert position["price_update_status"] == "not_in_latest_scan"
    assert position["price_update_checked_at"] == next_scan.isoformat()
    assert not [event for event in account["ledger"] if event["type"] == "SELL"]

    summary = paper_trading.account_summary(account, next_scan)
    assert summary["stale_price_positions"] == 1
    assert position["price_freshness"] == "STALE"
    assert position["price_age_seconds"] == 31 * 60


def test_account_summary_reads_coinbase_allowance_from_explicit_utc_month():
    account = {
        "starting_cash": 250.0,
        "cash": 250.0,
        "positions": [],
        "closed_trades": [],
        "coinbase_one_monthly_volume": {"2026-11": 500.0, "2026-12": 120.0},
    }
    summary = paper_trading.account_summary(
        account, datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)
    )

    assert summary["coinbase_one_monthly_volume"] == 120.0
    assert summary["coinbase_one_remaining_allowance"] == 380.0


def test_open_position_unrealized_pnl_deducts_estimated_exit_fee(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    signal = _signal()
    signal["asset_type"] = "crypto"
    candidate = _candidate(10.0)
    candidate["asset_type"] = "crypto"
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)

    account = paper_trading.update_account([candidate], [signal], now)
    position = account["positions"][0]
    expected_exit_fee = paper_trading.calculate_trade_fee(
        "crypto", "sell", position["quantity"] * 10.0, position["quantity"]
    )["total"]
    summary = paper_trading.account_summary(account, now)

    assert position["estimated_exit_fee"] == expected_exit_fee
    assert summary["estimated_exit_fees"] == round(expected_exit_fee, 2)
    assert summary["unrealized_pnl"] == round(
        -position["entry_fee_remaining"] - expected_exit_fee, 2
    )
    expected_equity = account["cash"] + position["quantity"] * 10.0 - expected_exit_fee
    assert summary["equity"] == round(expected_equity, 2)
    assert summary["total_return_pct"] == round((expected_equity / 250.0 - 1) * 100, 2)


def test_gap_directly_to_target_two_closes_full_position_at_observed_scan_price(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], now)
    initial_qty = account["positions"][0]["quantity"]

    # The next completed scan is already beyond both targets. The simulator
    # records the verified sampled price; it does not invent a fill at target 2.
    account = paper_trading.update_account([_candidate(12.5)], [_signal()], now)

    assert account["positions"] == []
    assert len(account["closed_trades"]) == 1
    trade = account["closed_trades"][0]
    assert trade["close_reason"] == "TARGET_2"
    assert trade["close_price"] == 12.5
    assert trade["quantity"] == 0.0
    sells = [event for event in account["ledger"] if event["type"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["quantity"] == initial_qty
    assert sells[0]["price"] == 12.5
    assert "observed" in sells[0]["message"]


def test_gap_below_stop_uses_verified_sampled_price_and_stop_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_candidate(10.0)], [_signal()], now)

    # With no intrabar path available, the observed verified scan price decides.
    # A sampled price at/below the stop takes the conservative stop branch.
    account = paper_trading.update_account([_candidate(8.5)], [_signal()], now)

    assert account["positions"] == []
    trade = account["closed_trades"][0]
    assert trade["close_reason"] == "STOP"
    assert trade["close_price"] == 8.5
    sell = [event for event in account["ledger"] if event["type"] == "SELL"][0]
    assert sell["price"] == 8.5
    assert "STOP observed" in sell["message"]


def _ranked_signal(symbol, score, at):
    signal = _signal()
    signal["symbol"] = symbol
    signal["signal_id"] = "stock:{}:{}".format(symbol, at)
    signal["score"] = score
    signal["at"] = at
    return signal


def _ranked_candidate(symbol, price=10.0):
    candidate = _candidate(price)
    candidate["symbol"] = symbol
    return candidate


def test_allocation_ranks_highest_official_buy_score_before_older_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    signals = [
        _ranked_signal("OLDER", 70, "2026-11-19T09:00:00+00:00"),
        _ranked_signal("STRONG", 95, "2026-11-19T10:00:00+00:00"),
        _ranked_signal("MIDDLE", 82, "2026-11-19T11:00:00+00:00"),
    ]
    candidates = [_ranked_candidate(symbol) for symbol in ("OLDER", "STRONG", "MIDDLE")]

    account = paper_trading.update_account(candidates, signals, datetime.now(timezone.utc))

    assert [p["symbol"] for p in account["positions"]] == ["STRONG", "MIDDLE", "OLDER"]
    assert [p["allocation_rank"] for p in account["positions"]] == [1, 2, 3]
    assert [row["official_score"] for row in account["allocation_review"]] == [95, 82, 70]
    assert account["positions"][0]["quality_risk_multiplier"] == 0.95
    assert account["positions"][0]["quantity"] > account["positions"][-1]["quantity"]
    assert account["allocation_review"][0]["effective_risk_budget_pct"] == 0.95
    assert all(row["status"] == "allocated" for row in account["allocation_review"])


def test_capacity_deferred_buy_is_reconsidered_after_a_position_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    signals = [
        _ranked_signal("ONE", 95, "2026-11-19T09:00:00+00:00"),
        _ranked_signal("TWO", 90, "2026-11-19T09:01:00+00:00"),
        _ranked_signal("THREE", 85, "2026-11-19T09:02:00+00:00"),
        _ranked_signal("FOUR", 80, "2026-11-19T09:03:00+00:00"),
        _ranked_signal("FIVE", 75, "2026-11-19T09:04:00+00:00"),
    ]
    candidates = [_ranked_candidate(symbol) for symbol in ("ONE", "TWO", "THREE", "FOUR", "FIVE")]
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)

    first = paper_trading.update_account(candidates, signals, now)
    assert len(first["positions"]) == paper_trading.MAX_OPEN_POSITIONS
    deferred = next(row for row in first["allocation_review"] if row["symbol"] == "FIVE")
    assert deferred["status"] == "deferred"
    assert deferred["rank"] == 5
    assert signals[4]["signal_id"] not in first["processed_signal_ids"]

    next_candidates = [_ranked_candidate("ONE", 8.9)] + [
        _ranked_candidate(symbol) for symbol in ("TWO", "THREE", "FOUR", "FIVE")
    ]
    second = paper_trading.update_account(next_candidates, signals, now)

    assert len(second["positions"]) == paper_trading.MAX_OPEN_POSITIONS
    assert {p["symbol"] for p in second["positions"]} == {"TWO", "THREE", "FOUR", "FIVE"}
    assert signals[4]["signal_id"] in second["processed_signal_ids"]
    assert second["allocation_review"][0]["symbol"] == "FIVE"
    assert second["allocation_review"][0]["status"] == "allocated"



def _risk_off_candidate(price=10.0):
    candidate = _candidate(price)
    candidate.update({
        "market_regime": "RISK-OFF", "score": 90,
        "score_model": "hybrid_offline_v2",
        "data_quality": {"buy_eligible": True},
        "levels": {
            "watch_entry_low": 9.5, "watch_entry_high": 10.5,
            "stop_loss": 9.0, "target_1": 11.0, "target_2": 12.0,
            "target_2_reward_risk_ratio": 3.0,
        },
        "buy_readiness": {"checks": {
            "score_82": True, "volume_confirmed": True, "breakout_confirmed": True,
            "two_technical_confirmations": True, "market_not_risk_off": False,
            "fresh_intraday_confirmation": True, "five_bar_intraday_trend_up": True,
            "no_risk_flags": True, "not_chase_risk": True,
            "price_verified": True, "live_data_quality": True,
        }},
    })
    return candidate


def test_risk_off_diagnostics_explain_failed_gates_without_relaxing_them():
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    eligible = _risk_off_candidate()
    blocked = _risk_off_candidate()
    blocked["symbol"] = "BLOCKED"
    blocked["buy_readiness"]["checks"]["volume_confirmed"] = False
    unverified = _risk_off_candidate()
    unverified["symbol"] = "UNVERIFIED"
    unverified["price_validation_passed"] = False

    diagnostics = {}
    signals = paper_trading.build_risk_off_paper_signals(
        [eligible, blocked, unverified], now, diagnostics
    )

    assert [signal["symbol"] for signal in signals] == ["TEST"]
    assert diagnostics["candidates_total"] == 3
    assert diagnostics["risk_off_candidates"] == 3
    assert diagnostics["eligible_signals"] == 1
    assert diagnostics["rejections"]["failed_buy_gate:volume_confirmed"] == 1
    assert diagnostics["rejections"]["price_not_independently_verified"] == 1


def test_risk_off_experimental_paper_entry_bypasses_only_regime_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_risk_off_candidate()], [], now)
    position = account["positions"][0]
    assert len(account["positions"]) == 1
    assert position["strategy"] == "risk_off_experimental"
    assert position["experimental"] is True
    assert position["market_regime_at_entry"] == "RISK-OFF"
    assert position["notional"] <= 62.5
    assert account["cash"] < 250
    assert account["ledger"][0]["strategy"] == "risk_off_experimental"
    summary = paper_trading.account_summary(account, now)
    assert summary["risk_off_open_positions"] == 1
    assert summary["risk_off_total_trades"] == 1


def test_risk_off_experimental_paper_entry_rejects_any_other_failed_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    candidate = _risk_off_candidate()
    candidate["buy_readiness"]["checks"]["volume_confirmed"] = False
    account = paper_trading.update_account([candidate], [], datetime.now(timezone.utc))
    assert account["cash"] == 250
    assert account["positions"] == []
    assert account["ledger"] == []


def test_risk_off_experimental_paper_entry_requires_verified_live_eligible_price(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    candidate = _risk_off_candidate()
    candidate["price_validation_passed"] = False
    account = paper_trading.update_account([candidate], [], datetime.now(timezone.utc))
    assert account["cash"] == 250
    assert account["positions"] == []
    assert account["ledger"] == []


def test_risk_off_signal_is_idempotent_across_repeated_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    candidate = _risk_off_candidate()
    first = paper_trading.update_account([candidate], [], now)
    second = paper_trading.update_account([candidate], [], now)
    assert len(second["positions"]) == 1
    assert second["cash"] == first["cash"]
    assert len([e for e in second["ledger"] if e.get("strategy") == "risk_off_experimental"]) == 1


def test_official_buy_signal_path_remains_separately_labeled(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    candidate = _candidate(10.0)
    candidate["market_regime"] = "RISK-ON"
    account = paper_trading.update_account([candidate], [_signal()], now)
    assert len(account["positions"]) == 1
    assert account["positions"][0]["strategy"] == "official_buy"
    assert account["positions"][0]["experimental"] is False



def test_risk_off_candidate_requires_regime_gate_to_be_the_failed_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    candidate = _risk_off_candidate()
    candidate["buy_readiness"]["checks"]["market_not_risk_off"] = True
    account = paper_trading.update_account([candidate], [], datetime.now(timezone.utc))
    assert account["cash"] == 250
    assert account["positions"] == []


def _early_entry_candidate(symbol="EARLY", price=10.0, verified=True, regime="NORMAL"):
    return {
        "asset_type": "stock",
        "symbol": symbol,
        "stage": "EARLY SETUP",
        "score": 70,
        "score_model": "hybrid_offline_v2",
        "price": price,
        "price_validation_passed": verified,
        "price_validation_status": "verified" if verified else "unverified",
        "price_as_of": "2026-11-19T15:00:00+00:00",
        "price_source": "test live feed",
        "market_regime": regime,
        "breakout_proximity_pct": 1.0,
        "data_quality": {
            "data_type": "live", "age_seconds": 30,
            "session": "REGULAR SESSION", "buy_eligible": True,
        },
        "buy_readiness": {"percent": 55, "checks": {"score_82": False}},
        "levels": {
            "watch_entry_low": 9.5, "watch_entry_high": 10.5,
            "stop_loss": 9.0, "target_1": 11.0, "target_2": 12.0,
            "target_2_reward_risk_ratio": 2.0,
        },
    }


def test_early_entry_signal_requires_verified_quality_and_valid_research_stage():
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    candidate = _early_entry_candidate()
    diagnostics = {}
    signals = paper_trading.build_early_entry_paper_signals([candidate], now, diagnostics)
    assert len(signals) == 1
    assert signals[0]["strategy"] == "early_entry_experimental"
    assert signals[0]["experimental"] is True
    assert signals[0]["research_classification"] == "PRE-BREAKOUT WATCH"
    assert signals[0]["experiment_version"] == "early-entry-v1"
    assert signals[0]["stop_loss"] == 9.0
    assert diagnostics["eligible_signals"] == 1

    unverified = _early_entry_candidate("UNVERIFIED", verified=False)
    risk_off = _early_entry_candidate("RISKOFF", regime="RISK-OFF")
    unknown_regime = _early_entry_candidate("UNKNOWN", regime="UNKNOWN")
    stale_stock = _early_entry_candidate("STALE")
    stale_stock["data_quality"]["age_seconds"] = 1200
    official = _early_entry_candidate("OFFICIAL")
    official["stage"] = "BUY"
    rejected = paper_trading.build_early_entry_paper_signals(
        [unverified, risk_off, unknown_regime, stale_stock, official], now
    )
    assert rejected == []


def test_early_entry_experiment_uses_existing_shared_account_and_one_state_file(tmp_path, monkeypatch):
    state_path = tmp_path / "paper.json"
    monkeypatch.setattr(paper_trading, "STATE_PATH", state_path)
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_early_entry_candidate()], [], now, enable_early_entry_experimental=True)

    assert account["starting_cash"] == 250.0
    assert account["cash"] < 250.0
    assert len(account["positions"]) == 1
    position = account["positions"][0]
    assert position["strategy"] == "early_entry_experimental"
    assert position["experimental"] is True
    assert position["research_classification"] == "PRE-BREAKOUT WATCH"
    assert position["entry_research_snapshot"]["price_verified"] is True
    assert account["ledger"][0]["strategy"] == "early_entry_experimental"
    assert state_path.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["paper.json"]
    summary = paper_trading.account_summary(account, now)
    strategy = summary["strategy_performance"][0]
    assert strategy["strategy"] == "early_entry_experimental"
    assert strategy["open_positions"] == 1
    assert summary["early_entry_total_trades"] == 1


def test_early_entry_same_day_signal_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    first = paper_trading.update_account([_early_entry_candidate()], [], now, enable_early_entry_experimental=True)
    cash_after_entry = first["cash"]
    second = paper_trading.update_account([_early_entry_candidate(price=10.1)], [], now, enable_early_entry_experimental=True)
    assert len(second["positions"]) == 1
    assert second["cash"] == cash_after_entry
    assert len([event for event in second["ledger"] if event["type"] == "BUY"]) == 1


def test_official_buy_is_allocated_before_new_early_entry_experiments(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    official_candidate = _candidate(10.0)
    official_candidate["stage"] = "BUY"
    early_candidate = _early_entry_candidate()
    account = paper_trading.update_account(
        [official_candidate, early_candidate], [_signal()], now, enable_early_entry_experimental=True
    )
    assert account["allocation_review"][0]["strategy"] == "official_buy"
    assert account["positions"][0]["strategy"] == "official_buy"
    assert any(p["strategy"] == "early_entry_experimental" for p in account["positions"])


def test_early_entry_experiments_remain_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = paper_trading.update_account([_early_entry_candidate()], [], now)
    assert account["cash"] == 250.0
    assert account["positions"] == []
    assert account["early_entry_signal_diagnostics"]["enabled"] is False
    assert paper_trading.account_summary(account, now)["early_entry_experimental_enabled"] is False
