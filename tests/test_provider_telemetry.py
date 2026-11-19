from datetime import timedelta
from types import SimpleNamespace

import main


def test_http_telemetry_counts_429_by_provider():
    main.reset_scan_metrics()
    response = SimpleNamespace(
        url="https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
        status_code=429,
        elapsed=timedelta(milliseconds=125),
        headers={"Retry-After": "2"},
    )

    assert main.record_http_response(response) is response
    snapshot = main.scan_metrics_snapshot()

    assert snapshot["provider_http_429_count"] == 1
    assert snapshot["provider_http_status_counts"] == {"binance:429": 1}
    metric = snapshot["providers"]["http_binance_status_429"]
    assert metric["calls"] == 1
    assert metric["failures"] == 1
    assert "Retry-After=2" in next(iter(metric["error_counts"]))


def test_http_telemetry_counts_server_errors():
    main.reset_scan_metrics()
    response = SimpleNamespace(
        url="https://api.kraken.com/0/public/Ticker",
        status_code=503,
        elapsed=timedelta(milliseconds=80),
        headers={},
    )

    main.record_http_response(response)
    snapshot = main.scan_metrics_snapshot()

    assert snapshot["provider_http_5xx_count"] == 1
    assert snapshot["provider_http_status_counts"] == {"kraken:503": 1}

def test_kraken_requests_are_paced(monkeypatch):
    sleeps = []
    calls = []
    clock = [100.0]

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(main.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    monkeypatch.setattr(main.HTTP, "get", fake_get)
    monkeypatch.setattr(main, "KRAKEN_LAST_REQUEST_AT", 100.0)

    result = main.kraken_get("https://api.kraken.com/0/public/Ticker", timeout=5)

    assert result is not None
    assert calls == [("https://api.kraken.com/0/public/Ticker", {"timeout": 5})]
    assert sleeps == [main.KRAKEN_MIN_REQUEST_INTERVAL_SECONDS]



def test_alpaca_snapshot_filter_skips_only_confirmed_unsupported_symbol():
    requested, skipped = main._alpaca_snapshot_request_symbols(["GTN-A", "BRK-B", "AAPL", "GTN-A"])

    assert requested == ["BRK-B", "AAPL"]
    assert skipped == ["GTN-A"]
