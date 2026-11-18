import unittest
import pandas as pd

from early_runner import compute_early_runner_metrics, summarize_crypto_cross_section


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


class CryptoCrossSectionTests(unittest.TestCase):
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
