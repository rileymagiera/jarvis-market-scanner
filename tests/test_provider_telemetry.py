from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests

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


@pytest.mark.parametrize(
    ("statuses", "retry_after", "expected_times", "expected_status"),
    [
        ([200, 200], None, [100.0, 100.2], 200),
        ([429, 200], "2", [100.0, 102.0], 200),
        ([429, 429], None, [100.0, 101.0], 429),
        ([429], "60", [100.0], 429),
        ([404], None, [100.0], 404),
    ],
)
def test_coinbase_exchange_request_pacing_and_retries(
    monkeypatch, statuses, retry_after, expected_times, expected_status
):
    clock = 100.0
    request_times = []
    responses = iter(statuses)

    def sleep(seconds):
        nonlocal clock
        clock += seconds

    def get(url, **kwargs):
        request_times.append(clock)
        response = requests.Response()
        response.url = url
        response.status_code = next(responses)
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return response

    monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock, sleep=sleep))
    monkeypatch.setattr(main, "COINBASE_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr(main.HTTP, "get", get)
    if expected_status == 200:
        assert main.coinbase_exchange_get("/products/BTC-USD/ticker").status_code == 200
        if statuses[0] == 200:
            assert main.coinbase_exchange_get("/products/ETH-USD/stats").status_code == 200
    else:
        with pytest.raises(requests.HTTPError) as exc:
            main.coinbase_exchange_get("/products/QCAD-USD/ticker")
        assert exc.value.response.status_code == expected_status
    assert request_times == pytest.approx(expected_times)


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


def test_coinbase_full_fallback_requires_fresh_quote_and_kraken_agreement(monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    candles = [[int(datetime.now(timezone.utc).timestamp()) - i * 3600, 99, 102, 98, 100, 10] for i in range(30)]
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/ticker"):
            return Response({"price": "100.20", "time": now})
        if url.endswith("/stats"):
            return Response({"open": "98", "volume": "1200"})
        if url.endswith("/candles"):
            return Response(candles)
        raise AssertionError(url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    ticker, bars, provider = main.coinbase_crypto_market_data("BTCUSDT", reference_price=100.0)

    assert provider == "Coinbase Exchange public API"
    assert ticker["quote_currency"] == "USD"
    assert ticker["price_validation_passed"] is True
    assert ticker["price_validation_method"] == "coinbase_timestamp_kraken_agreement"
    assert ticker["price_cross_check_difference_pct"] <= 0.5
    assert len(bars) == 30
    assert len(calls) == 3


def test_coinbase_full_fallback_without_kraken_reference_is_watch_only(monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    candles = [[int(datetime.now(timezone.utc).timestamp()) - i * 3600, 99, 102, 98, 100, 10] for i in range(30)]

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if url.endswith("/ticker"):
            return Response({"price": "100.20", "time": now})
        if url.endswith("/stats"):
            return Response({"open": "98", "volume": "1200"})
        if url.endswith("/candles"):
            return Response(candles)
        raise AssertionError(url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    ticker, _, _ = main.coinbase_crypto_market_data("BTCUSDT")

    assert ticker["price_validation_passed"] is False
    assert ticker["price_validation_method"] == "coinbase_fallback_unconfirmed"
    assert "watch-only" in ticker["price_validation_note"]


def test_coinbase_full_fallback_rejects_price_divergence(monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    candles = [[int(datetime.now(timezone.utc).timestamp()) - i * 3600, 99, 102, 98, 100, 10] for i in range(30)]

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if url.endswith("/ticker"):
            return Response({"price": "102.00", "time": now})
        if url.endswith("/stats"):
            return Response({"open": "98", "volume": "1200"})
        if url.endswith("/candles"):
            return Response(candles)
        raise AssertionError(url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    ticker, _, _ = main.coinbase_crypto_market_data("BTCUSDT", reference_price=100.0)

    assert ticker["price_validation_passed"] is False
    assert ticker["price_cross_check_difference_pct"] > 0.5


def test_coinbase_full_fallback_rejects_stale_quote(monkeypatch):
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    candles = [[int(datetime.now(timezone.utc).timestamp()) - i * 3600, 99, 102, 98, 100, 10] for i in range(30)]

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if url.endswith("/ticker"):
            return Response({"price": "100.20", "time": stale})
        if url.endswith("/stats"):
            return Response({"open": "98", "volume": "1200"})
        if url.endswith("/candles"):
            return Response(candles)
        raise AssertionError(url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    try:
        main.coinbase_crypto_market_data("BTCUSDT", reference_price=100.0)
    except RuntimeError as exc:
        assert "stale or future-dated" in str(exc)
    else:
        raise AssertionError("Stale Coinbase fallback quote must be rejected")


def test_kraken_different_pairs_can_make_concurrent_trade_requests(monkeypatch):
    """Independent markets do not need to wait on a single global REST lock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    both_in_flight = threading.Barrier(2, timeout=3)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["params"]["pair"]))
        both_in_flight.wait()
        return object()

    monkeypatch.setattr(main, "KRAKEN_PAIR_SCOPED_PACING", True)
    monkeypatch.setattr(main, "KRAKEN_PAIR_LAST_REQUEST_AT", {})
    monkeypatch.setattr(main, "KRAKEN_PAIR_REQUEST_LOCKS", {})
    monkeypatch.setattr(main.HTTP, "get", fake_get)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: main.kraken_get(f"{main.KRAKEN}/Trades", params={"pair": pair}),
            ("XBTUSD", "ETHUSD"),
        ))

    assert len(results) == 2
    assert {pair for _, pair in calls} == {"XBTUSD", "ETHUSD"}


def test_kraken_same_pair_trades_and_ohlc_keep_shared_spacing(monkeypatch):
    """Even different endpoints must pace repeated requests for one pair."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    started = []
    ended = []

    def fake_get(url, **kwargs):
        started.append(time.monotonic())
        time.sleep(0.01)
        ended.append(time.monotonic())
        return object()

    monkeypatch.setattr(main, "KRAKEN_PAIR_SCOPED_PACING", True)
    monkeypatch.setattr(main, "KRAKEN_PAIR_LAST_REQUEST_AT", {})
    monkeypatch.setattr(main, "KRAKEN_PAIR_REQUEST_LOCKS", {})
    monkeypatch.setattr(main, "KRAKEN_MIN_REQUEST_INTERVAL_SECONDS", 0.025)
    monkeypatch.setattr(main.HTTP, "get", fake_get)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(main.kraken_get, f"{main.KRAKEN}/{endpoint}", params={"pair": "XBTUSD"})
            for endpoint in ("Trades", "OHLC")
        ]
        for future in futures:
            assert future.result() is not None

    assert len(started) == len(ended) == 2
    assert started[1] >= ended[0]
    assert started[1] - ended[0] >= 0.020


def test_kraken_unscoped_public_endpoints_remain_serial(monkeypatch):
    """IP-wide endpoints must retain conservative serialization."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def fake_get(url, **kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["active"], state["peak"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return object()

    monkeypatch.setattr(main, "KRAKEN_PAIR_SCOPED_PACING", True)
    monkeypatch.setattr(main, "KRAKEN_LAST_REQUEST_AT", 0.0)
    monkeypatch.setattr(main, "KRAKEN_MIN_REQUEST_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(main.HTTP, "get", fake_get)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(
            lambda name: main.kraken_get(f"{main.KRAKEN}/{name}"),
            ("Ticker", "AssetPairs"),
        ):
            assert result is not None

    assert state["peak"] == 1


def test_kraken_pair_scoped_pacing_can_be_disabled(monkeypatch):
    """An emergency environment toggle restores fully serialized REST calls."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def fake_get(url, **kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["active"], state["peak"])
        time.sleep(0.01)
        with guard:
            state["active"] -= 1
        return object()

    monkeypatch.setattr(main, "KRAKEN_PAIR_SCOPED_PACING", False)
    monkeypatch.setattr(main, "KRAKEN_LAST_REQUEST_AT", 0.0)
    monkeypatch.setattr(main, "KRAKEN_MIN_REQUEST_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(main.HTTP, "get", fake_get)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda pair: main.kraken_get(f"{main.KRAKEN}/Trades", params={"pair": pair}),
            ("XBTUSD", "ETHUSD"),
        ))

    assert state["peak"] == 1
