import copy
import unittest

from runner_case_studies import build_runner_case_studies
from historical_audit_export import build_historical_audit_export


class RunnerCaseStudiesTests(unittest.TestCase):
    def sample(self):
        return {
            "asset_type": "stock", "symbol": "LVWR",
            "first_seen": "2026-11-19T14:45:00+00:00",
            "last_seen": "2026-11-19T23:00:00+00:00",
            "last_stage": "EARLY SETUP",
            "price_validation_status": "last_known",
            "discovery_baseline_status": "live_verified",
            "discovery_initial_price": 0.40,
            "discovery_price_as_of": "2026-11-19T14:44:00+00:00",
            "discovery_max_gain_pct": 68.0,
            "discovery_return_status": "provisional",
            "buy_gate_history": [
                {"at": "2026-11-19T15:05:00+00:00", "score": 30, "stage": "EARLY SETUP",
                 "price": 0.45, "failed_gate_keys": ["score_82", "market_not_risk_off"],
                 "market_regime": "RISK-OFF"},
                {"at": "2026-11-19T15:20:00+00:00", "score": 40,
                 "failed_gate_keys": ["score_82"]},
            ],
            "grade_history": [
                {"at": "2026-11-19T15:10:00+00:00",
                 "from": "LOW-CONVICTION WATCH", "to": "EARLY SETUP"}
            ],
            "signal_history": [
                {"observed_at": "2026-11-19T15:10:00+00:00",
                 "price_as_of": "2026-11-19T15:08:00+00:00", "price": 0.48,
                 "outcome_eligible": True, "price_verified": True,
                 "stage": "EARLY SETUP", "shadow_classification": "EARLY WATCH"},
                {"observed_at": "2026-11-19T23:00:00+00:00",
                 "price_as_of": "2026-11-19T05:00:00+00:00", "price": 0.67,
                 "outcome_eligible": True, "price_verified": True,
                 "stage": "EARLY SETUP", "shadow_classification": "EARLY WATCH"},
            ],
        }

    def test_existing_tracker_case_and_missing_stock(self):
        record = self.sample()
        original = copy.deepcopy(record)
        report = build_runner_case_studies([record], "lvwr, LVWR, KRNT")
        self.assertEqual(record, original)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["symbols"], ["LVWR", "KRNT"])
        self.assertEqual(len(report["cases"]), 2)
        first, missing = report["cases"]
        self.assertTrue(first["found"])
        self.assertEqual(first["first_buy_gate_check"]["score"], 30)
        self.assertEqual(first["first_buy_gate_check"]["failed_gate_keys"],
                         ["score_82", "market_not_risk_off"])
        self.assertEqual(first["first_stage_promotion"]["stage"], "EARLY SETUP")
        self.assertEqual(first["fresh_outcome_snapshots"], 1)
        self.assertEqual(first["rejected_legacy_outcome_flags"], 1)
        self.assertIsNone(first["verified_discovery_max_gain_pct"])
        self.assertFalse(missing["found"])
        self.assertIn("does not prove", missing["interpretation"])

    def test_existing_export_has_optional_on_demand_drilldown(self):
        record = self.sample()
        plain = build_historical_audit_export([record])
        requested = build_historical_audit_export([record], requested_symbols="LVWR")
        self.assertIsNone(plain["ticker_case_studies"])
        self.assertEqual(requested["ticker_case_studies"]["cases"][0]["symbol"], "LVWR")
        self.assertEqual(plain["production_effect"], "none")

    def test_rejects_invalid_or_unbounded_queries(self):
        for symbols in ["", " ", "ABC!", ",".join(str(x) for x in range(11))]:
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                build_runner_case_studies([], symbols)

    def test_crypto_case_uses_retained_verified_quote_only(self):
        record = {"asset_type": "crypto", "symbol": "BTCUSDT",
                  "first_seen": "2026-11-19T15:00:00+00:00",
                  "signal_history": [{"observed_at": "2026-11-19T15:00:00+00:00",
                                      "price_as_of": "2026-11-19T14:59:30+00:00",
                                      "price": 40000, "price_verified": True,
                                      "outcome_eligible": True}]}
        result = build_runner_case_studies([record], "BTCUSDT")["cases"][0]
        self.assertEqual(result["fresh_outcome_snapshots"], 1)
        self.assertEqual(result["asset_type"], "crypto")
        self.assertIsNone(result["first_buy_gate_check"])


if __name__ == "__main__":
    unittest.main()
