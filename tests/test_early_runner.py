import unittest
import pandas as pd

from early_runner import compute_early_runner_metrics


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


if __name__ == "__main__":
    unittest.main()
