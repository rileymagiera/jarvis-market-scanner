"""Webull read-only stock snapshots must use trade timestamps, not quote time."""
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
import sys

import pytest

import main


NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)


def ms(dt):
    return int(dt.timestamp() * 1000)


def stock_snapshot(**fields):
    return {
        "symbol": "AAPL",
        "price": "342.5600",
        "last_trade_time": ms(NOW - timedelta(minutes=21)),
        "quote_time": ms(NOW),
        **fields,
    }


def test_stock_snapshot_uses_fresh_extended_hours_trade():
    data = [stock_snapshot(
        extend_hour_last_price="344.8700",
        extend_hour_last_trade_time=ms(NOW - timedelta(seconds=20)),
    )]
    quote = main._webull_stock_snapshot_quote(data, "AAPL", 10, now=NOW)
    assert quote["price"] == 344.87
    assert quote["age_minutes"] == 0.3
    assert quote["as_of"] == (NOW - timedelta(seconds=20)).isoformat()


def test_regular_hours_trade_is_used_when_extended_price_is_missing():
    data = {"data": [stock_snapshot(
        price="177.01",
        last_trade_time=ms(NOW - timedelta(minutes=2)),
        extend_hour_last_price=None,
    )]}
    result = main._webull_stock_snapshot_quote(data, "AAPL", 10, now=NOW)
    assert result["price"] == 177.01
    assert result["age_minutes"] == 2


def test_older_extended_trade_does_not_override_newer_regular_trade():
    data = stock_snapshot(
        price="111.00", last_trade_time=ms(NOW - timedelta(seconds=30)),
        extend_hour_last_price="112.00",
        extend_hour_last_trade_time=ms(NOW - timedelta(minutes=2)),
    )
    assert main._webull_stock_snapshot_quote(data, "AAPL", 5, now=NOW)["price"] == 111.0


def test_snapshot_quote_time_does_not_make_stale_trade_look_live():
    data = stock_snapshot(price="142.00", quote_time=ms(NOW))
    with pytest.raises(RuntimeError, match="stale"):
        main._webull_stock_snapshot_quote([data], "AAPL", 10, now=NOW)


def test_snapshot_rejects_other_symbol_and_bad_trade_timestamp():
    data = [
        stock_snapshot(symbol="MSFT", last_trade_time=ms(NOW)),
        stock_snapshot(price="154.00", last_trade_time="not-a-timestamp"),
    ]
    with pytest.raises(RuntimeError, match="matching timestamped"):
        main._webull_stock_snapshot_quote(data, "AAPL", 10, now=NOW)


def test_snapshot_rejects_future_dated_trade():
    with pytest.raises(RuntimeError, match="future-dated"):
        main._webull_stock_snapshot_quote(
            [stock_snapshot(last_trade_time=ms(NOW + timedelta(minutes=7)))],
            "AAPL", 10, now=NOW,
        )


def test_snapshot_rejects_quote_time_only():
    data = {"symbol": "AAPL", "price": "100.00", "quote_time": ms(NOW)}
    with pytest.raises(RuntimeError, match="matching timestamped"):
        main._webull_stock_snapshot_quote(data, "AAPL", 10, now=NOW)


def _mock_sdk(monkeypatch, status, payload, calls):
    api_module = ModuleType("webull.core.client")
    data_module = ModuleType("webull.data.data_client")

    class FakeApi:
        def __init__(self, key, secret, region):
            assert (key, secret, region) == ("test-webull-key", "test-webull-secret", "us")

        def add_endpoint(self, *args):
            assert args == ("us", "api.webull.com")

    class FakeMarketData:
        def get_history_bar(self, *args, **kwargs):
            pytest.fail("Deprecated Webull historical-bar method must not be called")

        def get_snapshot(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(status_code=status, json=lambda: payload)

    class FakeDataClient:
        def __init__(self, api):
            self.market_data = FakeMarketData()

    api_module.ApiClient = FakeApi
    data_module.DataClient = FakeDataClient
    monkeypatch.setitem(sys.modules, "webull.core.client", api_module)
    monkeypatch.setitem(sys.modules, "webull.data.data_client", data_module)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-webull-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-webull-secret")
    main.WEBULL_QUOTE_CACHE.clear()


def test_direct_call_uses_read_only_snapshot_endpoint(monkeypatch):
    now = datetime.now(timezone.utc)
    calls = []
    payload = [stock_snapshot(
        last_trade_time=ms(now - timedelta(minutes=1)),
        extend_hour_last_price="344.87",
        extend_hour_last_trade_time=ms(now - timedelta(seconds=5)),
    )]
    _mock_sdk(monkeypatch, 200, payload, calls)
    quote = main._webull_latest_bar_direct("AAPL", "M15", 10)
    assert quote["price"] == 344.87
    assert len(calls) == 1
    assert calls[0] == (
        ("AAPL", "US_STOCK"),
        {"extend_hour_required": True, "overnight_required": False},
    )


def test_direct_call_reports_403_without_printing_server_response(monkeypatch):
    calls = []
    _mock_sdk(monkeypatch, 403, {"secret": "should-not-be-displayed"}, calls)
    with pytest.raises(PermissionError, match="HTTP 403") as failure:
        main._webull_latest_bar_direct("AAPL", "M1", 10)
    assert "should-not-be-displayed" not in str(failure.value)
    assert len(calls) == 1
