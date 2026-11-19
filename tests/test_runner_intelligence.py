import unittest

from runner_intelligence import build_runner_intelligence_report


class RunnerIntelligenceTests(unittest.TestCase):
    def test_missed_runner_requires_verified_discovery_and_no_buy_record(self):
        records = [
            {"asset_type": "stock", "symbol": "AAA", "discovery_return_status": "verified",
             "discovery_max_gain_pct": 31.2, "discovery_return_pct": 12.0,
             "discovery_max_drawdown_pct": -4.0, "price_validation_status": "verified"},
            {"asset_type": "stock", "symbol": "BBB", "discovery_return_status": "verified",
             "discovery_max_gain_pct": 40.0},
            {"asset_type": "crypto", "symbol": "CCC", "discovery_return_status": "provisional",
             "discovery_max_gain_pct": 55.0},
        ]
        report = build_runner_intelligence_report(records, [
            {"asset_type": "stock", "symbol": "BBB", "status": "stale"}
        ])
        candidates = report["missed_runner_tracker"]["candidates"]
        self.assertEqual([x["symbol"] for x in candidates], ["AAA"])
        self.assertEqual(candidates[0]["classification"],
                         "Watchlist runner; no matching official BUY record found")

    def test_signal_cohort_uses_frozen_verified_snapshots_and_marks_small_sample(self):
        records = []
        for i in range(3):
            records.append({
                "asset_type": "stock", "symbol": f"S{i}",
                "early_runner_snapshot_at": "2026-01-01T15:00:00+00:00",
                "session_aware_tracking": True,
                "early_runner_initial_feature_snapshot": {"early_warning_matches": ["Near breakout"]},
                "early_runner_initial_signal_matches": ["Near breakout"],
                "early_runner_outcomes": {"5d": {"return_pct": 25 if i == 0 else -2}},
                "research_max_favorable_pct": 30,
                "research_max_adverse_pct": -5,
            })
        records.append({
            "asset_type": "stock", "symbol": "OLD",
            "early_runner_snapshot_at": "2026-01-01T15:00:00+00:00",
            "session_aware_tracking": False,
            "early_runner_initial_feature_snapshot": {},
            "early_runner_initial_signal_matches": ["Near breakout"],
            "early_runner_outcomes": {"5d": {"return_pct": 80}},
        })
        report = build_runner_intelligence_report(records)
        cohort = report["signal_quality_by_setup"]["cohorts"][0]
        self.assertEqual(cohort["observations"], 3)
        self.assertEqual(cohort["runner_count"], 1)
        self.assertEqual(cohort["sample_status"], "early sample · do not promote")

    def test_radar_excludes_unscored_and_unsurfaced_candidates(self):
        records = [
            {"asset_type": "crypto", "symbol": "BTC", "currently_surfaced": True,
             "early_runner_research_score": 70, "early_runner_signal_matches": ["Volume expansion"]},
            {"asset_type": "stock", "symbol": "OLD", "currently_surfaced": False,
             "early_runner_research_score": 90},
            {"asset_type": "stock", "symbol": "NONE", "currently_surfaced": True},
        ]
        radar = build_runner_intelligence_report(records)["early_warning_radar"]
        self.assertEqual(radar["candidate_count"], 1)
        self.assertEqual(radar["candidates"][0]["symbol"], "BTC")


if __name__ == "__main__":
    unittest.main()
