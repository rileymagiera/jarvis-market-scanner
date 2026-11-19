import tempfile
import unittest
from pathlib import Path
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

    def test_persists_between_scanner_processes_within_same_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kraken.json"
            first = HourlyCandleCache(cache_path=path)
            first.put("XBTUSD", [[1, 2]], now_epoch=3601)
            second = HourlyCandleCache(cache_path=path)
            self.assertEqual(second.get("XBTUSD", now_epoch=3602), [[1, 2]])

    def test_persistent_cache_discards_previous_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kraken.json"
            first = HourlyCandleCache(cache_path=path)
            first.put("XBTUSD", [[1, 2]], now_epoch=3599)
            second = HourlyCandleCache(cache_path=path)
            self.assertIsNone(second.get("XBTUSD", now_epoch=3600))


if __name__ == "__main__":
    unittest.main()
