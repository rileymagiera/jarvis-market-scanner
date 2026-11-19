import unittest

from historical_audit_export import build_historical_audit_export


class HistoricalAuditExportTests(unittest.TestCase):
    def test_export_is_compact_read_only_and_splits_assets(self):
        records = [
            {"asset_type": "stock", "symbol": "AAA", "discovery_return_status": "verified",
             "discovery_max_gain_pct": 25.0, "currently_surfaced": True,
             "early_runner_research_score": 62, "early_runner_signal_matches": ["Volume acceleration"]},
            {"asset_type": "crypto", "symbol": "BBB", "discovery_return_status": "verified",
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
             "discovery_max_gain_pct": 22.0},
            {"asset_type": "crypto", "symbol": "BAD", "discovery_return_status": "provisional",
             "discovery_max_gain_pct": 90.0},
        ]
        before = [dict(row) for row in records]
        result = build_historical_audit_export(records)
        self.assertEqual(records, before)
        self.assertEqual([x["symbol"] for x in result["missed_runners"]["candidates"]], ["GOOD"])
        self.assertEqual(result["missed_runners"]["total_verified_watchlist_runners"], 1)

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
