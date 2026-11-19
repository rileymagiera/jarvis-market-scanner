import unittest
from market_data_cache import HourlyCandleCache


class HourlyCandleCacheTests(unittest.TestCase):
    def test_reuses_hourly_context_within_same_utc_hour(self):
        cache = HourlyCandleCache()
        cache.put("XBTUSD", [[1, 2]], now_epoch=3601)
        self.assertEqual(cache.get("XBTUSD", now_epoch=7199), [[1, 2]])

    def test_refreshes_at_utc_hour_boundary(self):
        cache = HourlyCandleCache()
        cache.put("XBTUSD", [[1, 2]], now_epoch=7199)
        self.assertIsNone(cache.get("XBTUSD", now_epoch=7200))

    def test_cache_returns_copy(self):
        cache = HourlyCandleCache()
        rows = [[1, 2]]
        cache.put("XBTUSD", rows, now_epoch=3601)
        result = cache.get("XBTUSD", now_epoch=3602)
        result.append([3, 4])
        self.assertEqual(cache.get("XBTUSD", now_epoch=3602), [[1, 2]])


if __name__ == "__main__":
    unittest.main()
