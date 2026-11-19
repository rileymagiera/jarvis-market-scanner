from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace

import main


def test_sec_request_starts_are_globally_paced(monkeypatch):
    clock = [100.0]
    sleeps = []

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(main.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    monkeypatch.setattr(main, "SEC_LAST_REQUEST_AT", 0.0)

    main.sec_wait_for_request_slot()
    main.sec_wait_for_request_slot()

    assert len(sleeps) == 1
    assert sleeps[0] == main.SEC_REQUEST_MIN_INTERVAL_SECONDS
    assert clock[0] == 100.0 + main.SEC_REQUEST_MIN_INTERVAL_SECONDS


def test_sec_ticker_map_is_loaded_once_for_concurrent_workers(monkeypatch):
    calls = []
    calls_lock = Lock()

    class FakeResponse:
        def __init__(self, is_mapping):
            self.is_mapping = is_mapping

        def raise_for_status(self):
            return None

        def json(self):
            if self.is_mapping:
                return {"0": {"ticker": "AAA", "cik_str": 1234}}
            return {"filings": {"recent": {}}}

    def fake_get(url, **kwargs):
        with calls_lock:
            calls.append(url)
        return FakeResponse("company_tickers.json" in url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    monkeypatch.setattr(main, "sec_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(main, "SEC_TICKER_MAP", None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: main.sec_recent_filings("AAA"), range(8)))

    assert results == [[] for _ in range(8)]
    assert sum("company_tickers.json" in url for url in calls) == 1
    assert sum("data.sec.gov/submissions/" in url for url in calls) == 8
