import unittest
import pandas as pd

from early_runner import compute_early_runner_metrics, summarize_crypto_cross_section, set_crypto_relative_strength_coverage, build_data_quality_record


class EarlyRunnerMetricsTests(unittest.TestCase):
    def test_catalyst_and_dilution_are_separated(self):
        daily = pd.DataFrame({"Close": [10.0, 10.5], "High": [10.2, 10.7], "Low": [9.8, 10.1], "Volume": [1000, 1200]})
        result = compute_early_runner_metrics(
            daily, pd.DataFrame(), 11.0,
            {"fields": {"float_shares": 1_000_000, "shortPercentOfFloat": 0.2}},
            [{"title": "Company announces FDA approval and registered direct offering", "form": "8-K"}],
        )
        self.assertIn("FDA approval", result["catalyst_labels"])
        self.assertTrue(result["dilution_flags"])
        self.assertEqual(result["float_shares"], 1_000_000)
        self.assertGreaterEqual(result["early_runner_research_score"], 0)

    def test_empty_intraday_is_explicit_and_safe(self):
        daily = pd.DataFrame({"Close": [10.0, 10.5], "High": [10.2, 10.7], "Low": [9.8, 10.1], "Volume": [1000, 1200]})
        result = compute_early_runner_metrics(daily, pd.DataFrame(), 10.5, {}, [])
        self.assertIsNone(result["float_rotation_pct"])
        self.assertIsNone(result["cumulative_rvol_same_time"])
        self.assertEqual(result["early_runner_research_score"], 0)


    def test_data_quality_distinguishes_live_delayed_eod_and_unknown(self):
        received = "2026-11-18T04:00:00+00:00"
        live = build_data_quality_record(
            "stock", "Yahoo + Alpaca", "fresh_multi_provider_cross_checked",
            "2026-11-18T03:59:00+00:00", received,
            {"phase": "REGULAR SESSION"}, True, "iex"
        )
        delayed = build_data_quality_record(
            "stock", "Yahoo + Massive", "fresh_multi_provider_cross_checked",
            "2026-11-18T03:59:00+00:00", received,
            {"phase": "REGULAR SESSION"}, True, None
        )
        eod = build_data_quality_record(
            "stock", "Yahoo + Alpaca prior-session daily close cross-check",
            "prior_session_close_cross_checked", "2026-11-17T21:00:00+00:00",
            received, {"phase": "CLOSED — OVERNIGHT"}, True, "iex"
        )
        unknown = build_data_quality_record(
            "stock", "Yahoo Finance daily; live quote unverified",
            "daily_bar_unverified", "2026-11-18T03:00:00+00:00",
            received, {"phase": "REGULAR SESSION"}, False
        )
        self.assertEqual(live["data_type"], "live")
        self.assertTrue(live["buy_eligible"])
        self.assertEqual(delayed["data_type"], "delayed")
        self.assertFalse(delayed["buy_eligible"])
        self.assertEqual(eod["data_type"], "eod")
        self.assertFalse(eod["buy_eligible"])
        self.assertEqual(unknown["data_type"], "historical")
        self.assertFalse(unknown["buy_eligible"])

    def test_research_stages_survive_non_live_data_but_buy_is_blocked(self):
        delayed = {"data_type": "delayed", "buy_eligible": False}
        blocked = apply_entry_quality_gate("BUY", delayed, True, {"all_gates_passed": False})
        self.assertEqual(blocked["stage"], "ACTIONABLE SETUP")
        self.assertEqual(blocked["potential_stage"], "BUY")
        self.assertEqual(blocked["entry_status"], "WAITING FOR LIVE ENTRY CONFIRMATION")

        watch = apply_entry_quality_gate("EARLY SETUP", delayed, True, {})
        self.assertEqual(watch["stage"], "EARLY SETUP")
        self.assertEqual(watch["entry_status"], "RESEARCH ONLY — DELAYED DATA")

    def test_unverified_price_does_not_hide_research_candidate(self):
        result = apply_entry_quality_gate("DEVELOPING RUNNER", {"data_type": "unknown"}, False, {})
        self.assertEqual(result["stage"], "DEVELOPING RUNNER")
        self.assertEqual(result["entry_status"], "RESEARCH ONLY — PRICE UNVERIFIED")

    def test_crypto_live_timestamp_is_required_for_buy_eligibility(self):
        received = "2026-11-18T04:00:00+00:00"
        fresh = build_data_quality_record(
            "crypto", "Kraken Spot public API", "exchange_timestamped_ticker",
            "2026-11-18T03:59:00+00:00", received, {}, True, None, 60
        )
        stale = build_data_quality_record(
            "crypto", "Kraken Spot public API", "timestamp_missing_or_stale",
            "2026-11-18T03:40:00+00:00", received, {}, False, None, 1200
        )
        self.assertEqual(fresh["data_type"], "live")
        self.assertTrue(fresh["buy_eligible"])
        self.assertEqual(stale["verification_status"], "stale")
        self.assertFalse(stale["buy_eligible"])



class CryptoCrossSectionTests(unittest.TestCase):
    def test_legacy_and_canonical_relative_strength_flags_match(self):
        coverage = {"relative_strength_vs_btc": False, "market_relative_strength": False}
        set_crypto_relative_strength_coverage(coverage, 0.37)
        self.assertTrue(coverage["relative_strength_vs_btc"])
        self.assertTrue(coverage["market_relative_strength"])
        set_crypto_relative_strength_coverage(coverage, None)
        self.assertFalse(coverage["relative_strength_vs_btc"])
        self.assertFalse(coverage["market_relative_strength"])

    def test_btc_benchmark_exists_without_btc_candidate(self):
        sectors = {"BTCUSDT": "Bitcoin", "SOLUSDT": "Layer 1", "BNBUSDT": "Layer 1",
                   "AVAXUSDT": "Layer 1", "ADAUSDT": "Layer 1"}
        pairs = {"BTC": "XXBTZUSD", "SOL": "SOLUSD", "BNB": "BNBUSD",
                 "AVAX": "AVAXUSD", "ADA": "ADAUSD"}
        tickers = {
            "XXBTZUSD": {"c": ["90000"], "o": "100000"},
            "SOLUSD": {"c": ["105"], "o": "100"},
            "BNBUSD": {"c": ["210"], "o": "200"},
            "AVAXUSD": {"c": ["31.5"], "o": "30"},
            "ADAUSD": {"c": ["0.49"], "o": "0.50"},
        }
        result = summarize_crypto_cross_section(sectors, pairs, tickers)
        self.assertAlmostEqual(result["btc_change_pct"], -10.0, places=3)
        self.assertEqual(result["btc_source"], "Kraken full-market 24h ticker")
        self.assertTrue(result["sector_stats"]["Layer 1"]["reliable"])
        self.assertEqual(result["sector_stats"]["Layer 1"]["sample_size"], 4)

    def test_thin_sector_sample_is_not_reliable(self):
        sectors = {"SOLUSDT": "Layer 1", "BNBUSDT": "Layer 1",
                   "AVAXUSDT": "Layer 1", "ADAUSDT": "Layer 1"}
        pairs = {"SOL": "SOLUSD", "BNB": "BNBUSD", "AVAX": "AVAXUSD", "ADA": "ADAUSD"}
        tickers = {
            "SOLUSD": {"c": ["105"], "o": "100"},
            "BNBUSD": {"c": ["210"], "o": "200"},
        }
        result = summarize_crypto_cross_section(sectors, pairs, tickers)
        stats = result["sector_stats"]["Layer 1"]
        self.assertEqual(stats["sample_size"], 2)
        self.assertEqual(stats["coverage_pct"], 50.0)
        self.assertFalse(stats["reliable"])


if __name__ == "__main__":
    unittest.main()
