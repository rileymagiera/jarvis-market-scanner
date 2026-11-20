import copy
import unittest

from gate_combination_shadow import build_gate_combination_shadow
from historical_audit_export import build_historical_audit_export


T0 = "2026-11-19T10:00:00+00:00"
T1 = "2026-11-19T11:00:00+00:00"


def event(failed, *, at=T0, verified=True, followups=1, favorable=8.0, adverse=-2.0):
    return {
        "at": at, "price": 1.2, "price_verified": verified,
        "failed_gate_keys": list(failed), "market_regime": "RISK-OFF" if
        "market_not_risk_off" in failed else "NEUTRAL",
        "follow_up_observations": followups,
        "max_favorable_since_pct": favorable,
        "max_adverse_since_pct": adverse,
    }


def profile(report, name):
    return next(row for row in report["profiles"] if row["name"] == name)


class GateCombinationShadowTests(unittest.TestCase):
    def test_combined_gates_reuse_history_without_changing_candidate(self):
        rows = [
            {"asset_type": "crypto", "symbol": "AAAUSDT", "buy_gate_history": [
                event(["score_82", "24h_breakout"], favorable=12, adverse=-4)
            ]},
            {"asset_type": "crypto", "symbol": "BBBUSDT", "buy_gate_history": [
                event(["score_82", "24h_breakout", "above_24h_average"])
            ]},
        ]
        original = copy.deepcopy(rows)
        report = build_gate_combination_shadow(rows)
        result = profile(report, "crypto_score_volume_breakout")
        self.assertEqual(result["newly_qualifying_unique_candidates"], 1)
        self.assertEqual(result["newly_qualifying_with_measured_followup"], 1)
        self.assertEqual(result["sampled_later_reached_10pct"], 1)
        self.assertEqual(result["sampled_adverse_at_least_3pct"], 1)
        self.assertEqual(result["still_blocked_by_other_gates"], 1)
        self.assertEqual(result["top_remaining_blockers"][0]["gate"], "above_24h_average")
        self.assertEqual(profile(report, "crypto_score_only")["newly_qualifying_unique_candidates"], 0)
        self.assertEqual(rows, original)
        self.assertEqual(report["production_effect"], "none")

    def test_explicit_risk_off_research_never_treated_as_standard_eligibility(self):
        rows = [
            {"asset_type": "crypto", "symbol": "RISKUSDT", "buy_gate_history": [
                event(["score_82", "market_not_risk_off"], favorable=20)
            ]}
        ]
        report = build_gate_combination_shadow(rows)
        self.assertEqual(profile(report, "crypto_score_only")["newly_qualifying_unique_candidates"], 0)
        special = profile(report, "crypto_score_breakout_risk_off_shadow")
        self.assertEqual(special["newly_qualifying_unique_candidates"], 1)
        self.assertTrue(special["regime_gate_relaxed"])
        self.assertEqual(special["sampled_later_reached_20pct"], 1)

    def test_unverified_risky_and_no_new_gate_failure_excluded(self):
        rows = [
            {"asset_type": "crypto", "symbol": "NOQUOTE", "buy_gate_history": [
                event(["score_82"], verified=False)
            ]},
            {"asset_type": "crypto", "symbol": "RISKY", "buy_gate_history": [
                event(["score_82", "not_extended"])
            ]},
            {"asset_type": "crypto", "symbol": "PASS", "buy_gate_history": [
                event([])
            ]},
        ]
        report = build_gate_combination_shadow(rows)
        result = profile(report, "crypto_score_only")
        self.assertEqual(result["newly_qualifying_unique_candidates"], 0)
        self.assertEqual(result["candidate_symbols_with_at_least_one_relaxed_gate_failed"], 0)

    def test_first_eligible_event_cannot_be_replaced_with_later_winner(self):
        rows = [
            {"asset_type": "crypto", "symbol": "AAAUSDT", "buy_gate_history": [
                event(["score_82"], at=T1, favorable=50, followups=2),
                event(["score_82"], at=T0, favorable=None, adverse=None, followups=0),
            ]},
        ]
        result = profile(build_gate_combination_shadow(rows), "crypto_score_only")
        self.assertEqual(result["newly_qualifying_unique_candidates"], 1)
        self.assertEqual(result["newly_qualifying_with_measured_followup"], 0)
        self.assertEqual(result["sampled_later_reached_10pct"], 0)
        self.assertEqual(result["sample_status"], "insufficient_unique_followup")

    def test_asset_specific_gates_and_no_new_paper_trading_state(self):
        rows = [
            {"asset_type": "stock", "symbol": "STOCK", "buy_gate_history": [
                event(["volume_confirmed", "breakout_confirmed"], favorable=6)
            ]},
            {"asset_type": "crypto", "symbol": "COINUSDT", "buy_gate_history": [
                event(["volume_1_8x", "24h_breakout"], favorable=7)
            ]},
        ]
        result = build_historical_audit_export(rows, [])
        self.assertEqual(result["export_version"], "1.3")
        self.assertTrue(result["read_only"])
        self.assertNotIn("paper_tracker", result)
        self.assertNotIn("paper_trading", result)
        shadow = result["gate_combination_shadow"]
        self.assertEqual(profile(shadow, "stock_score_volume_breakout")["newly_qualifying_unique_candidates"], 1)
        self.assertEqual(profile(shadow, "crypto_score_volume_breakout")["newly_qualifying_unique_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
