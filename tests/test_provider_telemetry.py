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


def test_coinbase_secondary_validation_accepts_fresh_matching_quote(monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"price": "100.20", "time": now}

    calls = []
    monkeypatch.setattr(main.HTTP, "get", lambda url, **kwargs: (calls.append((url, kwargs)) or Response()))
    result = main.coinbase_crypto_price_validation("BTCUSDT", 100.0)

    assert result["passed"] is True
    assert result["difference_pct"] < 0.5
    assert result["age_seconds"] <= 1
    assert calls[0][0].endswith("/products/BTC-USD/ticker")
    assert calls[0][1]["timeout"] == (2, 5)


def test_coinbase_secondary_validation_rejects_price_divergence(monkeypatch):
    from datetime import datetime, timezone

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"price": "102.00", "time": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: Response())
    result = main.coinbase_crypto_price_validation("BTCUSDT", 100.0)

    assert result["passed"] is False
    assert result["difference_pct"] > 0.5


def test_coinbase_secondary_validation_rejects_stale_ticker(monkeypatch):
    from datetime import datetime, timedelta, timezone

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"price": "100.10", "time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}

    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: Response())
    result = main.coinbase_crypto_price_validation("BTCUSDT", 100.0)

    assert result["passed"] is False
    assert result["age_seconds"] > 300



def test_binance_us_shadow_quote_handles_ticker_without_bid_ask(monkeypatch):
    from datetime import datetime, timezone

    close_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"lastPrice": "100.25", "closeTime": close_time_ms}

    calls = []
    monkeypatch.setattr(
        main.HTTP,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or Response()),
    )

    quote = main.crypto_shadow_quote("binance_us", "BTCUSDT", "USD")

    assert quote["status"] == "fresh"
    assert quote["price"] == 100.25
    assert quote["bid"] is None
    assert quote["ask"] is None
    assert quote["spread_pct"] is None
    assert calls[0][1]["params"] == {"symbol": "BTCUSD"}


def test_binance_us_shadow_quote_rejects_missing_price_cleanly(monkeypatch):
    from datetime import datetime, timezone

    close_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"closeTime": close_time_ms}

    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: Response())

    try:
        main.crypto_shadow_quote("binance_us", "BTCUSDT", "USD")
    except RuntimeError as exc:
        assert "no usable price or exchange timestamp" in str(exc)
    else:
        raise AssertionError("Missing Binance price should be rejected")
