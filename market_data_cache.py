"""Small thread-safe cache for Kraken's stable hourly OHLC context."""
from __future__ import annotations
import threading
import time


class HourlyCandleCache:
    def __init__(self, max_entries=300):
        self._items = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries

    @staticmethod
    def hour_bucket(now_epoch=None):
        return int((time.time() if now_epoch is None else now_epoch) // 3600)

    def get(self, key, now_epoch=None):
        bucket = self.hour_bucket(now_epoch)
        with self._lock:
            item = self._items.get(key)
            if not item or item["hour_bucket"] != bucket:
                return None
            return list(item["rows"])

    def put(self, key, rows, now_epoch=None):
        bucket = self.hour_bucket(now_epoch)
        with self._lock:
            self._items[key] = {"hour_bucket": bucket, "rows": list(rows)}
            if len(self._items) > self.max_entries:
                stale = [k for k, v in self._items.items() if v["hour_bucket"] != bucket]
                for old_key in stale:
                    self._items.pop(old_key, None)
                while len(self._items) > self.max_entries:
                    self._items.pop(next(iter(self._items)))

    def clear(self):
        with self._lock:
            self._items.clear()
