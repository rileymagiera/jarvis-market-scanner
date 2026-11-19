import unittest

from buy_intelligence import build_buy_intelligence, blend_buy_score


class BuyIntelligenceMappingTests(unittest.TestCase):
    def test_stock_maps_scanner_features_into_model(self):
        item = {
            "asset_type": "stock",
            "technical_analysis": {"bollinger_squeeze": True},
            "base_tightness_pct": 5.0,
            "atr_contraction_pct": 15.0,
            "higher_highs_higher_lows": True,
            "breakout_20d": True,
            "relative_strength_vs_spy": 3.2,
            "relative_strength_vs_iwm": 1.4,
            "five_day_change_pct": 8.0,
            "intraday_change_pct": 1.5,
            "above_session_vwap": True,
            "relative_volume": 2.1,
            "intraday_relative_volume": 2.3,
            "cumulative_rvol_same_time": 2.0,
            "volume_acceleration_3bar": 2.1,
            "premarket_rvol_same_time": 1.8,
            "float_rotation_pct": 25.0,
            "catalyst_labels": ["FDA catalyst"],
            "dilution_flags": [],
            "news": [],
        }
        result = build_buy_intelligence(item)
        self.assertEqual(result["version"], "shadow_v3")
        self.assertGreater(result["components"]["early_formation"], 0)
        self.assertGreater(result["components"]["momentum_confirmation"], 0)
        self.assertGreater(result["components"]["unusual_activity"], 0)
        self.assertEqual(result["component_coverage"]["momentum_confirmation"]["available"], 25)
        self.assertFalse(result["affects_production_buy_score"])
        self.assertFalse(result["can_execute_trades"])

    def test_crypto_maps_btc_relative_strength_and_crypto_features(self):
        item = {
            "asset_type": "crypto",
            "technical_analysis": {},
            "crqs_relative_strength_vs_btc_24h_pct": 3.0,
            "crqs_features": {
                "four_hour_change_pct": 2.0,
                "volume_consistency_6h": 0.85,
                "higher_lows": True,
            },
            "hourly_change_pct": 1.0,
            "above_24h_average": True,
            "relative_volume": 2.0,
            "breakout_24h": True,
            "news": [],
        }
        result = build_buy_intelligence(item)
        self.assertGreater(result["components"]["early_formation"], 0)
        self.assertGreater(result["components"]["momentum_confirmation"], 0)
        self.assertGreater(result["components"]["unusual_activity"], 0)
        self.assertEqual(result["component_coverage"]["momentum_confirmation"]["available"], 25)
        self.assertFalse(result["affects_production_buy_score"])
        self.assertFalse(result["can_execute_trades"])

    def test_early_warning_evidence_maps_into_existing_score_families(self):
        result = build_buy_intelligence({
            "asset_type": "stock",
            "breakout_proximity_pct": 1.2,
            "volatility_compression_percentile": 12.0,
            "multi_timeframe_alignment": True,
            "liquidity_sweep_detected": True,
            "accumulation_signal_score": 100,
            "news": [],
        })
        coverage = result["component_coverage"]
        self.assertEqual(coverage["early_formation"]["available"], 8)
        self.assertEqual(coverage["early_formation"]["possible"], 38)
        self.assertEqual(coverage["momentum_confirmation"]["available"], 5)
        self.assertEqual(coverage["unusual_activity"]["available"], 8)
        self.assertGreater(result["components"]["early_formation"], 0)
        self.assertGreater(result["components"]["momentum_confirmation"], 0)
        self.assertGreater(result["components"]["unusual_activity"], 0)
        self.assertEqual(result["version"], "shadow_v3")

    def test_missing_inputs_are_not_scored_as_failures(self):
        result = build_buy_intelligence({"asset_type": "stock"})
        self.assertIsNone(result["score"])
        self.assertTrue(all(value is None for value in result["components"].values()))
        self.assertEqual(result["historical_confidence"], "insufficient")

    def test_historical_confidence_requires_a_usable_historical_score(self):
        result = build_buy_intelligence({
            "asset_type": "stock",
            "historical_setup_evidence": {"observations": 40},
        })
        self.assertEqual(result["historical_confidence"], "insufficient")
        self.assertIsNone(result["components"]["historical_edge"])

    def test_coverage_qualified_pilot_blends_scores_at_twenty_percent(self):
        intelligence = {
            "score": 100,
            "maximum_available_points": 85,
            "component_coverage": {
                "early_formation": {"available": 38, "possible": 38},
                "momentum_confirmation": {"available": 30, "possible": 30},
                "unusual_activity": {"available": 33, "possible": 33},
                "catalyst_quality": {"available": 1, "possible": 1},
                "historical_edge": {"available": 0, "possible": 1},
            },
        }
        result = blend_buy_score(70, intelligence)
        self.assertTrue(result["applied"])
        self.assertEqual(result["score"], 76)
        self.assertEqual(result["legacy_score"], 70)
        self.assertEqual(result["weight"], 0.20)
        self.assertEqual(result["model"], "hybrid_offline_v2")

    def test_insufficient_coverage_keeps_legacy_score(self):
        intelligence = {
            "score": 100,
            "maximum_available_points": 85,
            "component_coverage": {
                "early_formation": {"available": 5, "possible": 38},
                "momentum_confirmation": {"available": 5, "possible": 30},
                "unusual_activity": {"available": 2, "possible": 33},
                "catalyst_quality": {"available": 0, "possible": 1},
            },
        }
        result = blend_buy_score(70, intelligence)
        self.assertFalse(result["applied"])
        self.assertEqual(result["score"], 70)
        self.assertEqual(result["model"], "legacy_fallback")


if __name__ == "__main__":
    unittest.main()
