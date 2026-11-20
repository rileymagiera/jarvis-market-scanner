import unittest

from historical_audit_export import build_historical_audit_export


class HistoricalAuditExportTests(unittest.TestCase):
    def test_export_is_compact_read_only_and_splits_assets(self):
        records = [
            {"asset_type": "stock", "symbol": "AAA", "discovery_return_status": "verified",
             "discovery_baseline_status": "live_verified", "discovery_initial_price": 10.0,
             "discovery_price_as_of": "2026-11-19T20:00:00+00:00",
             "discovery_max_gain_pct": 25.0, "currently_surfaced": True,
             "early_runner_research_score": 62, "early_runner_signal_matches": ["Volume acceleration"]},
            {"asset_type": "crypto", "symbol": "BBB", "discovery_return_status": "verified",
             "discovery_baseline_status": "live_verified",
             "discovery_initial_price": 1.0, "discovery_price_as_of": "2026-11-19T20:00:00+00:00",
             "discovery_max_gain_pct": 35.0, "currently_surfaced": True,
             "early_runner_research_score": 71, "early_runner_signal_matches": ["Higher lows"]},
        ]
        result = build_historical_audit_export(
            records, [], last_scan_completed_at="2026-11-19T20:45:00+00:00",
            scan_run_id=124, history_source="render_snapshot_fast",
        )
        self.assertTrue(result["read_only"])
        self.assertEqual(result["production_effect"], "none")
        self.assertEqual(result["missed_runners"]["included_candidates_by_asset"], {"stocks": 1, "crypto": 1})
        self.assertEqual(result["provenance"]["scan_run_id"], "124")
        self.assertNotIn("paper_tracker", result)
        self.assertNotIn("paper_trading", result)

    def test_export_does_not_mutate_records_and_excludes_unverified_misses(self):
        records = [
            {"asset_type": "stock", "symbol": "GOOD", "discovery_return_status": "verified",
             "discovery_baseline_status": "live_verified", "discovery_initial_price": 5.0,
             "discovery_price_as_of": "2026-11-19T20:00:00+00:00",
             "discovery_max_gain_pct": 22.0},
            {"asset_type": "crypto", "symbol": "BAD", "discovery_return_status": "provisional",
             "discovery_baseline_status": "live_verified", "discovery_initial_price": 0.5,
             "discovery_price_as_of": "2026-11-19T20:00:00+00:00",
             "discovery_max_gain_pct": 90.0},
        ]
        before = [dict(row) for row in records]
        result = build_historical_audit_export(records)
        self.assertEqual(records, before)
        self.assertEqual([x["symbol"] for x in result["missed_runners"]["candidates"]], ["GOOD"])
        self.assertEqual(result["missed_runners"]["total_verified_watchlist_runners"], 1)

    def test_export_embeds_read_only_time_audit_without_raw_snapshots(self):
        stamp0 = "2026-11-19T15:00:00+00:00"
        stamp1 = "2026-11-19T15:15:00+00:00"
        record = {"asset_type": "crypto", "symbol": "XYZUSDT", "signal_history": [
            {"scan_id": "one", "observed_at": stamp0, "price_as_of": stamp0,
             "price_verified": True, "outcome_eligible": True,
             "price": 2, "shadow_classification": "EARLY ACCUMULATION"},
            {"scan_id": "two", "observed_at": stamp1, "price_as_of": stamp1,
             "price_verified": True, "outcome_eligible": True,
             "price": 2.2, "shadow_classification": "BREAKOUT DEVELOPING"},
        ]}
        output = build_historical_audit_export([record])
        audit = output["signal_lead_time"]
        self.assertEqual(audit["mode"], "read_only_research")
        self.assertEqual(audit["eligible_snapshots"], 2)
        self.assertEqual(audit["candidates_with_followup"], 1)
        self.assertIn("entry_timing_lab", output)
        self.assertEqual(output["entry_timing_lab"]["mode"], "read_only_shadow_analysis")
        self.assertEqual(output["entry_timing_lab"]["events_without_next_fresh_scan"], 1)
        self.assertNotIn("signal_history", str(output))
        self.assertEqual(len(record["signal_history"]), 2)

    def test_official_buy_record_excludes_matching_symbol(self):
        records = [{"asset_type": "crypto", "symbol": "AAA",
                    "discovery_return_status": "verified",
                    "discovery_baseline_status": "live_verified", "discovery_initial_price": 2.0,
                    "discovery_price_as_of": "2026-11-19T20:00:00+00:00",
                    "discovery_max_gain_pct": 40}]
        result = build_historical_audit_export(
            records, [{"asset_type": "crypto", "symbol": "AAA"}]
        )
        self.assertEqual(result["missed_runners"]["total_verified_watchlist_runners"], 0)


if __name__ == "__main__":
    unittest.main()
