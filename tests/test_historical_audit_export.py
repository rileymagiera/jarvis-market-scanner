import unittest

from early_opportunity_radar import build_candidate_snapshot
from historical_audit_export import build_historical_audit_export


class HistoricalAuditExportTests(unittest.TestCase):
    def test_stock_execution_audit_uses_quote_timestamp_even_for_delayed_signal_price(self):
        observed = "2026-11-19T15:00:00+00:00"
        levels = {"watch_entry_low": 9.9, "watch_entry_high": 10.4,
                  "stop_loss": 9.8, "target_1": 10.5, "target_2": 10.9}
        item = {"asset_type": "stock", "stage": "EARLY SETUP", "price": 10,
                "price_as_of": "2026-11-19T14:50:00+00:00", "price_validation_status": "verified",
                "data_quality": {"data_type": "delayed"}, "levels": levels}
        quote = {"feed": "iex", "as_of": "2026-11-19T14:59:45+00:00",
                 "bid": 9.98, "ask": 10, "ask_size": 300}
        rows = []
        for symbol, changes in (
            ("WITHIN", {}),
            ("ABOVE", {"ask": 10.2}),
            ("STALE", {"as_of": "2026-11-19T14:58:59+00:00"}),
            ("DELAY", {"feed": "delayed_sip"}),
            ("CROSSED", {"bid": 10.1}),
        ):
            snapshot = build_candidate_snapshot(
                {**item, "stock_execution_quote": {**quote, **changes}}, observed, "current"
            )
            self.assertFalse(snapshot["outcome_eligible"])
            rows.append({"asset_type": "stock", "symbol": symbol, "signal_history": [snapshot]})
        rows.append({"asset_type": "stock", "symbol": "LEGACY", "signal_history": [
            build_candidate_snapshot(item, observed, "current"),
        ]})
        rows.append({"asset_type": "stock", "symbol": "PREVIOUS", "signal_history": [
            build_candidate_snapshot(item, observed, "previous"),
        ]})
        audit = build_historical_audit_export(rows, scan_run_id="current")["stock_execution_quotes"]
        self.assertEqual(audit["stock_research_snapshots"], 6)
        self.assertEqual(audit["counts"], {
            "ask_within_modeled_range": 1, "ask_above_maximum": 1, "quote_too_old_or_untimed": 1,
            "unsupported_feed": 1, "unusable_bid_ask": 1, "no_saved_quote": 1,
        })
        self.assertEqual(audit["examples"][0]["quote_as_of"], quote["as_of"])
        self.assertEqual(audit["examples"][0]["quote_age_seconds"], 15)
        self.assertEqual(audit["examples"][0]["ask_size"], 300)
        self.assertEqual(audit["examples"][0]["binding_upper_limit"], "net_reward_to_risk")
        selected = build_historical_audit_export(
            rows, scan_run_id="current", requested_symbols="STALE"
        )["stock_execution_quotes"]
        self.assertEqual(selected["counts"], audit["counts"])
        self.assertEqual([example["symbol"] for example in selected["examples"]], ["STALE"])
        self.assertEqual(selected["examples"][0]["status"], "quote_too_old_or_untimed")

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
