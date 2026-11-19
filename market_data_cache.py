"""Thread-safe, optionally persistent cache for completed Kraken hourly OHLC context."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class HourlyCandleCache:
    def __init__(self, max_entries=300, cache_path=None):
        self._items = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self.cache_path = Path(cache_path) if cache_path else None
        self._load()

    @staticmethod
    def hour_bucket(now_epoch=None):
        return int((time.time() if now_epoch is None else now_epoch) // 3600)

    def _load(self):
        if self.cache_path is None:
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            bucket = self.hour_bucket()
            entries = payload.get("entries") if isinstance(payload, dict) else {}
            if not isinstance(entries, dict):
                return
            self._items = {
                str(key): {"hour_bucket": int(value["hour_bucket"]), "rows": value["rows"]}
                for key, value in entries.items()
                if isinstance(value, dict)
                and int(value.get("hour_bucket", -1)) == bucket
                and isinstance(value.get("rows"), list)
            }
            if len(self._items) > self.max_entries:
                self._items = dict(list(self._items.items())[-self.max_entries:])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._items = {}

    def _save(self):
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "entries": self._items}
            temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.cache_path)
        except (OSError, TypeError, ValueError):
            # Cache persistence is an optimization; provider fetches remain authoritative.
            return

    @staticmethod
    def _key(key):
        """Return a stable JSON-safe key; Kraken uses (pair_id, interval) tuples."""
        if isinstance(key, str):
            return key
        return json.dumps(key, sort_keys=True, separators=(",", ":"))

    def get(self, key, now_epoch=None):
        bucket = self.hour_bucket(now_epoch)
        key = self._key(key)
        with self._lock:
            item = self._items.get(key)
            if not item or item["hour_bucket"] != bucket:
                return None
            return list(item["rows"])

    def put(self, key, rows, now_epoch=None):
        bucket = self.hour_bucket(now_epoch)
        key = self._key(key)
        with self._lock:
            self._items[key] = {"hour_bucket": bucket, "rows": list(rows)}
            if len(self._items) > self.max_entries:
                stale = [k for k, v in self._items.items() if v["hour_bucket"] != bucket]
                for old_key in stale:
                    self._items.pop(old_key, None)
                while len(self._items) > self.max_entries:
                    self._items.pop(next(iter(self._items)))
            self._save()

    def clear(self):
        with self._lock:
            self._items.clear()
            self._save()
