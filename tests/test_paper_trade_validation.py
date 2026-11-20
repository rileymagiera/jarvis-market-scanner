"""Tests for read-only Jarvis paper-trade validation."""
import copy
import math
import unittest

from paper_trade_validation import build_paper_trade_validation


def trade(pnl, *, symbol="X", strategy="risk_off_experimental", regime="RISK-OFF",
          asset="crypto", at="2026-11-19T14:00:00+00:00", profile=None):
    result = {
        "symbol": symbol, "realized_pnl": pnl, "strategy": strategy,
        "market_regime_at_entry": regime, "asset_type": asset, "closed_at": at,
    }
    if profile is not None:
        result["paper_edge_profile"] = profile
    return result


class PaperTradeValidationTests(unittest.TestCase):
    def test_empty_account_has_no_fabricated_win_rate(self):
        result = build_paper_trade_validation({"closed_trades": []})
        self.assertEqual(result["overall"]["closed_trades"], 0)
        self.assertIsNone(result["overall"]["win_rate_pct"])
        self.assertIsNone(result["overall"]["win_rate_wilson_95_pct"])
        self.assertIsNone(result["overall"]["profit_factor"])
        self.assertEqual(result["by_strategy"], [])

    def test_two_risk_off_losses_stay_insufficient(self):
        account = {"closed_trades": [
            trade(-2.14328163, symbol="MEGAUSDT"),
            trade(-2.58763807, symbol="GRASSUSDT", at="2026-11-19T16:00:00+00:00"),
        ]}
        initial = copy.deepcopy(account)
        result = build_paper_trade_validation(account)
        self.assertEqual(account, initial, "Report must never mutate paper account")
        row = result["overall"]
        self.assertEqual(row["wins"], 0)
        self.assertEqual(row["losses"], 2)
        self.assertEqual(row["win_rate_pct"], 0.0)
        self.assertEqual(row["net_realized_pnl"], -4.7309)
        self.assertEqual(row["sample_status"], "insufficient_sample")
        self.assertGreater(row["win_rate_wilson_95_pct"][1], 50)
        self.assertEqual(result["by_strategy_regime"][0]["entry_regime"], "RISK-OFF")
        self.assertEqual(row["longest_closed_losing_streak"], 2)

    def test_groups_only_frozen_profiles_and_preserves_strategies(self):
        frozen = {"version": "github-confluence-v1", "cohort": "trend_volatility_confluence"}
        account = {"closed_trades": [
            trade(5, strategy="official_buy", regime="RISK-ON", asset="stock", profile=frozen),
            trade(-2, strategy="early_entry_experimental", regime="RISK-ON", asset="crypto"),
            trade(0, strategy="official_buy", regime="RISK-ON", asset="stock"),
        ]}
        result = build_paper_trade_validation(account)
        row = result["overall"]
        self.assertEqual((row["wins"], row["losses"], row["breakeven"]), (1, 1, 1))
        self.assertEqual(row["win_rate_pct"], 33.33)
        self.assertEqual(row["net_realized_pnl"], 3.0)
        self.assertEqual(row["profit_factor"], 2.5)
        self.assertEqual(len(result["by_frozen_edge_cohort"]), 1)
        self.assertEqual(result["by_frozen_edge_cohort"][0]["closed_trades"], 1)
        self.assertEqual(len(result["by_strategy"]), 2)
        self.assertEqual(len(result["by_strategy_asset"]), 2)

    def test_rejects_missing_nan_infinity_and_boolean_pnl(self):
        result = build_paper_trade_validation({"closed_trades": [
            trade(None), trade(float("nan")), trade(float("inf")), trade(True),
            trade(-1), None, {},
        ]})
        self.assertEqual(result["reported_closed_trades"], 7)
        self.assertEqual(result["valid_closed_trades"], 1)
        self.assertEqual(result["excluded_invalid_pnl"], 6)
        self.assertEqual(result["overall"]["net_realized_pnl"], -1)

    def test_closed_drawdown_uses_exit_sequence_not_input_order(self):
        result = build_paper_trade_validation({"closed_trades": [
            trade(-3, at="2026-11-19T16:00:00+00:00"),
            trade(5, at="2026-11-19T15:00:00+00:00"),
            trade(-2, at="2026-11-19T17:00:00+00:00"),
        ]})
        self.assertEqual(result["overall"]["closed_pnl_max_drawdown"], 5)
        self.assertEqual(result["overall"]["longest_closed_losing_streak"], 2)

    def test_full_win_cohort_does_not_report_infinite_profit_factor(self):
        result = build_paper_trade_validation({"closed_trades": [trade(1), trade(2)]})
        self.assertIsNone(result["overall"]["profit_factor"])
        self.assertEqual(result["overall"]["win_rate_pct"], 100)
        self.assertLess(result["overall"]["win_rate_wilson_95_pct"][0], 40)


if __name__ == "__main__":
    unittest.main()
