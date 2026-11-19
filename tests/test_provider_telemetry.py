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
