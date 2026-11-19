import unittest

from early_opportunity_radar import (
    MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE,
    append_candidate_snapshot,
    build_candidate_snapshot,
    build_shadow_opportunity_radar,
)


class EarlyOpportunityRadarTests(unittest.TestCase):
    def stock(self, **updates):
        item = {
            "asset_type": "stock", "symbol": "ABC", "price": 10.0,
            "stage": "EARLY SETUP", "score": 55,
            "price_validation_status": "verified", "price_validation_passed": True,
            "price_as_of": "2026-11-19T15:00:00+00:00",
            "data_quality": {"data_type": "live", "age_seconds": 30, "session": "REGULAR SESSION"},
            "early_runner_research_score": 42,
            "volume_acceleration_3bar": 1.8, "cumulative_rvol_same_time": 1.5,
            "buy_readiness": {"percent": 45, "checks": {"score_82": False}},
            "early_runner_signal_matches": ["Intraday volume acceleration"],
        }
        item.update(updates)
        return item

    def test_builds_research_only_snapshot_and_classification(self):
        snapshot = build_candidate_snapshot(self.stock(), "2026-11-19T15:00:00+00:00", "run-1")
        self.assertEqual(snapshot["shadow_classification"], "EARLY ACCUMULATION")
        self.assertTrue(snapshot["price_verified"])
        self.assertTrue(snapshot["outcome_eligible"])
        self.assertEqual(snapshot["failed_gate_keys"], ["score_82"])

    def test_unverified_price_is_captured_but_not_used_for_outcomes(self):
        item = self.stock(price_validation_status="unverified", price_validation_passed=False)
        snapshot = build_candidate_snapshot(item, "2026-11-19T15:00:00+00:00", "run-1")
        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot["price_verified"])
        self.assertFalse(snapshot["outcome_eligible"])

    def test_duplicate_scan_id_replaces_instead_of_duplicating(self):
        record = {}
        item = self.stock()
        append_candidate_snapshot(record, item, "2026-11-19T15:00:00+00:00", "same-run")
        append_candidate_snapshot(record, self.stock(score=61), "2026-11-19T15:01:00+00:00", "same-run")
        self.assertEqual(len(record["signal_history"]), 1)
        self.assertEqual(record["signal_history"][0]["score"], 61)

    def test_history_is_bounded(self):
        record = {}
        for index in range(MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE + 5):
            item = self.stock(score=50 + index)
            append_candidate_snapshot(record, item, f"2026-11-19T15:{index:02d}:00+00:00", f"run-{index}")
        self.assertEqual(len(record["signal_history"]), MAX_SIGNAL_SNAPSHOTS_PER_CANDIDATE)
        self.assertEqual(record["signal_history"][0]["scan_id"], "run-5")

    def test_shadow_report_measures_only_eligible_observations(self):
        record = {"asset_type": "stock", "symbol": "ABC"}
        first = self.stock(price=10.0)
        second = self.stock(price=11.0)
        append_candidate_snapshot(record, first, "2026-11-19T15:00:00+00:00", "run-1")
        append_candidate_snapshot(record, second, "2026-11-19T15:15:00+00:00", "run-2")
        append_candidate_snapshot(record, self.stock(price=12.0, price_validation_status="unverified", price_validation_passed=False),
                                  "2026-11-19T15:30:00+00:00", "run-3")
        report = build_shadow_opportunity_radar([record])
        candidate = report["candidates"][0]
        self.assertEqual(report["snapshots_recorded"], 3)
        self.assertEqual(report["outcome_eligible_snapshots"], 2)
        self.assertEqual(candidate["max_favorable_since_first_eligible_pct"], 10.0)
        self.assertEqual(candidate["outcome_sample_count"], 2)

    def test_missing_optional_fields_are_safe(self):
        snapshot = build_candidate_snapshot(
            {"asset_type": "crypto", "symbol": "XYZ", "early_warning_matches": ["Higher lows"]},
            "2026-11-19T15:00:00+00:00", "run-missing"
        )
        self.assertIsNotNone(snapshot)
        self.assertIsNone(snapshot["price"])
        self.assertFalse(snapshot["price_verified"])
        self.assertFalse(snapshot["outcome_eligible"])

    def test_outcome_cohorts_separate_asset_and_data_quality(self):
        record = {"asset_type": "crypto", "symbol": "XYZ"}
        append_candidate_snapshot(record, {
            "asset_type": "crypto", "symbol": "XYZ", "price": 2.0,
            "stage": "EARLY WATCH", "price_validation_status": "verified",
            "price_validation_passed": True, "early_warning_matches": ["Higher lows"],
            "data_quality": {"data_type": "live"},
        }, "2026-11-19T15:00:00+00:00", "crypto-1")
        append_candidate_snapshot(record, {
            "asset_type": "crypto", "symbol": "XYZ", "price": 2.2,
            "stage": "DEVELOPING SETUP", "price_validation_status": "verified",
            "price_validation_passed": True, "early_warning_matches": ["Higher lows"],
            "data_quality": {"data_type": "live"},
        }, "2026-11-19T15:15:00+00:00", "crypto-2")
        cohort = build_shadow_opportunity_radar([record])["outcome_cohorts"][0]
        self.assertEqual(cohort["asset_type"], "crypto")
        self.assertEqual(cohort["data_quality_cohort"], "live")
        self.assertEqual(cohort["reached_5pct"], 1)

    def test_snapshot_does_not_mutate_candidate_or_buy_gate(self):
        item = self.stock()
        original = dict(item)
        record = {}
        append_candidate_snapshot(record, item, "2026-11-19T15:00:00+00:00", "run-1")
        self.assertEqual(item, original)
        self.assertEqual(item["buy_readiness"]["percent"], 45)


if __name__ == "__main__":
    unittest.main()
