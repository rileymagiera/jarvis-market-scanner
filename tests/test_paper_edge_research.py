"""Paper-only confluence research regression tests (no live orders)."""
from datetime import datetime, timezone

import paper_edge_research
import paper_trading


def _early(symbol="MOMO", score=70, qualified=True):
    candidate = {
        "asset_type": "stock", "symbol": symbol, "stage": "EARLY SETUP",
        "score": score, "price": 10.0,
        "price_validation_passed": True, "price_validation_status": "verified",
        "price_as_of": "2026-11-19T15:00:00+00:00", "market_regime": "NORMAL",
        "breakout_proximity_pct": 1.0,
        "data_quality": {
            "data_type": "live", "age_seconds": 10,
            "session": "REGULAR SESSION", "buy_eligible": True,
        },
        "levels": {
            "watch_entry_low": 9.5, "watch_entry_high": 10.5,
            "stop_loss": 9.0, "target_1": 11.0, "target_2": 12.0,
        },
    }
    if qualified:
        candidate.update({
            "technical_analysis": {"ema_9_above_ema_20": True,
                                   "bollinger_squeeze_expansion": False,
                                   "obv_trend_up_10d": True},
            "atr_contraction_pct": 3.0,
            "five_day_change_pct": 5.0,
            "relative_strength_vs_spy": 2.5,
            "relative_volume": 1.8,
        })
    return candidate


def test_profile_uses_existing_soft_volatility_and_trend_evidence():
    result = paper_edge_research.paper_edge_profile(_early())
    assert result["qualified_trend_volatility"] is True
    assert result["cohort"] == "trend_volatility_confluence"
    assert result["factors"]["soft_volatility_expansion"] is True
    assert result["confirmed_factors"] >= 4
    assert "win_rate" not in result


def test_missing_data_stays_unknown_and_not_good():
    profile = paper_edge_research.paper_edge_profile(_early(qualified=False))
    assert profile["qualified_trend_volatility"] is False
    assert profile["available_factors"] == 0
    assert profile["cohort"] == "unconfirmed_or_incomplete"
    assert all(value is None for value in profile["factors"].values())


def test_crypto_is_analysed_separately_without_fabricating_stock_indicators():
    candidate = {
        "asset_type": "crypto",
        "multi_timeframe_alignment": True,
        "hourly_change_pct": 2.3,
        "crqs_relative_strength_vs_btc_24h_pct": 3.0,
        "relative_volume": 2.0,
    }
    profile = paper_edge_research.paper_edge_profile(candidate)
    assert profile["cohort"] == "relative_strength_confluence"
    assert profile["qualified_trend_volatility"] is False
    assert profile["factors"]["soft_volatility_expansion"] is None


def test_qualified_early_candidate_is_ranked_before_higher_scored_early_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    no_confluence = _early("HIGH", score=90, qualified=False)
    confluence = _early("CONF", score=65, qualified=True)
    account = paper_trading.update_account(
        [no_confluence, confluence], [], now, enable_early_entry_experimental=True
    )
    assert account["allocation_review"][0]["symbol"] == "CONF"
    assert account["allocation_review"][0]["edge_qualified"] is True
    assert account["allocation_review"][1]["symbol"] == "HIGH"
    assert account["positions"][0]["paper_edge_profile"]["cohort"] == "trend_volatility_confluence"
    assert account["paper_edge_diagnostics"]["screened_verified_candidates"] == 2
    assert account["paper_edge_diagnostics"]["trend_volatility_confluence_candidates"] == 1


def test_only_verified_buy_quality_candidates_enter_profile_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    verified = _early()
    rejected = _early("STALE")
    rejected["price_validation_passed"] = False
    result = paper_trading.update_account([verified, rejected], [], now)
    assert result["positions"] == []
    assert result["paper_edge_diagnostics"]["screened_verified_candidates"] == 1
    assert result["paper_edge_diagnostics"]["trend_volatility_confluence_candidates"] == 1


def test_cohort_reports_only_finished_versioned_paper_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    candidate = _early()
    opened = paper_trading.update_account(
        [candidate], [], now, enable_early_entry_experimental=True
    )
    opening_summary = paper_trading.account_summary(opened, now)
    cohort = opening_summary["paper_edge_cohort_performance"][0]
    assert cohort["closed_trades"] == 0
    assert cohort["win_rate_pct"] is None
    candidate["price"] = 12.1
    closed = paper_trading.update_account([candidate], [], now, enable_early_entry_experimental=False)
    closing_summary = paper_trading.account_summary(closed, now)
    cohort = closing_summary["paper_edge_cohort_performance"][0]
    assert cohort["closed_trades"] == 1
    assert cohort["wins"] == 1
    assert cohort["win_rate_pct"] == 100.0
    assert cohort["expectancy_per_closed_trade"] > 0
    assert cohort["profit_factor"] is None


def test_legacy_trade_without_profile_is_not_reclassified():
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    account = {
        "starting_cash": 250, "cash": 250, "positions": [], "ledger": [],
        "closed_trades": [{"asset_type": "stock", "entry_price": 10,
                           "realized_pnl": 1.0, "strategy": "official_buy"}],
    }
    summary = paper_trading.account_summary(account, now)
    assert summary["paper_edge_cohort_performance"] == []
