import main


def test_crypto_shadow_sample_default_is_50():
    assert main.CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN == 50


def test_crypto_shadow_sample_rotates_across_full_universe():
    symbols = [f"COIN{i}USD" for i in range(92)]

    first = main.select_crypto_shadow_symbols(symbols, run_number=1)
    second = main.select_crypto_shadow_symbols(symbols, run_number=2)

    assert len(first) == 50
    assert len(second) == 50
    assert len(set(first)) == 50
    assert len(set(second)) == 50
    assert set(first) | set(second) == set(symbols)


def test_crypto_shadow_sample_never_exceeds_configured_cap(monkeypatch):
    monkeypatch.setattr(main, "CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN", 50)
    symbols = [f"COIN{i}USD" for i in range(120)]

    selected = main.select_crypto_shadow_symbols(symbols, run_number=1)

    assert len(selected) == 50


def test_binance_us_bulk_shadow_tickers_load_complete_snapshot(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"symbol": f"TEST{i}USD", "lastPrice": "10", "closeTime": 1} for i in range(25)]

    calls = []
    monkeypatch.setattr(main.HTTP, "get", lambda url, **kwargs: (calls.append((url, kwargs)) or Response()))
    snapshot = main.fetch_binance_us_shadow_tickers()

    assert len(calls) == 1
    assert calls[0][0] == "https://api.binance.us/api/v3/ticker/24hr"
    assert "params" not in calls[0][1]
    assert snapshot["TEST1USD"]["lastPrice"] == "10"


def test_binance_us_shadow_batch_price_matches_individual_response(monkeypatch):
    from datetime import datetime, timezone

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    entry = {"symbol": "BTCUSD", "lastPrice": "102.25", "closeTime": now_ms}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return entry

    calls = []
    monkeypatch.setattr(main.HTTP, "get", lambda url, **kwargs: (calls.append((url, kwargs)) or Response()))
    original = main.crypto_shadow_quote("binance_us", "BTCUSDT", "USD")
    batched = main.crypto_shadow_quote("binance_us", "BTCUSDT", "USD", {"BTCUSD": entry})

    for key in ("status", "provider", "symbol", "quote_currency", "price", "as_of", "bid", "ask", "spread_pct"):
        assert batched[key] == original[key]
    assert len(calls) == 1


def test_shadow_batch_preserves_missing_symbol_as_unavailable_without_fake_price(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc)
    now_ms = int(stamp.timestamp() * 1000)
    snapshot = {"BTCUSD": {"symbol": "BTCUSD", "lastPrice": "100", "closeTime": now_ms}}
    monkeypatch.setattr(main, "BINANCE_US_SHADOW_BATCH_ENABLED", True)
    monkeypatch.setattr(main, "fetch_binance_us_shadow_tickers", lambda: snapshot)
    monkeypatch.setattr(main, "CRYPTO_SHADOW_HISTORY_PATH", str(tmp_path / "shadow.json"))
    calls = []

    class CoinbaseResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"price": "100", "time": stamp.isoformat(), "bid": "99.9", "ask": "100.1"}

    def fake_get(url, **kwargs):
        calls.append(url)
        assert "api.exchange.coinbase.com" in url
        return CoinbaseResponse()

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    results = [
        {"symbol": "BTCUSDT", "price_quote_currency": "USD", "price": 100, "price_as_of": stamp.isoformat()},
        {"symbol": "NOTLISTEDUSDT", "price_quote_currency": "USD", "price": 100, "price_as_of": stamp.isoformat()},
    ]

    report = main.collect_crypto_shadow_comparisons(
        results, run_number=1, run_id="sample", coinbase_quotes={"BTC": {"USD"}},
    )
    rows = report["recent_records"]
    assert report["symbols_sampled"] == 2
    assert report["records_this_scan"] == 3
    assert len(calls) == 1
    assert len(rows) == 3

    by_key = {(row["provider"], row["symbol"]): row for row in rows}
    assert by_key[("binance_us", "BTCUSDT")]["status"] == "comparable"
    assert by_key[("binance_us", "NOTLISTEDUSDT")]["status"] == "unavailable"
    assert by_key[("binance_us", "NOTLISTEDUSDT")]["shadow_price"] is None
    assert by_key[("coinbase", "BTCUSDT")]["status"] == "comparable"


def test_shadow_bulk_failure_falls_back_to_each_original_market_request(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc)
    now_ms = int(stamp.timestamp() * 1000)
    monkeypatch.setattr(main, "BINANCE_US_SHADOW_BATCH_ENABLED", True)
    monkeypatch.setattr(main, "CRYPTO_SHADOW_HISTORY_PATH", str(tmp_path / "shadow.json"))
    monkeypatch.setattr(main, "fetch_binance_us_shadow_tickers", lambda: (_ for _ in ()).throw(RuntimeError("bulk unavailable")))
    calls = []

    class Response:
        def __init__(self, is_binance):
            self.is_binance = is_binance

        def raise_for_status(self):
            pass

        def json(self):
            return (
                {"lastPrice": "100", "closeTime": now_ms}
                if self.is_binance
                else {"price": "100", "time": stamp.isoformat(), "bid": "99.9", "ask": "100.1"}
            )

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response("api.binance.us" in url)

    monkeypatch.setattr(main.HTTP, "get", fake_get)
    results = [
        {"symbol": "BTCUSDT", "price_quote_currency": "USD", "price": 100, "price_as_of": stamp.isoformat()},
        {"symbol": "ETHUSDT", "price_quote_currency": "USD", "price": 100, "price_as_of": stamp.isoformat()},
    ]
    report = main.collect_crypto_shadow_comparisons(
        results, run_number=1, run_id="fallback", coinbase_quotes={"BTC": {"USD"}, "ETH": {"USD"}},
    )

    assert report["records_this_scan"] == 4
    binance_requests = [x for x in calls if "api.binance.us" in x[0]]
    assert len(binance_requests) == 2
    assert all(x[1]["params"]["symbol"] in ("BTCUSD", "ETHUSD") for x in binance_requests)


def test_incomplete_binance_us_bulk_snapshot_rejected(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"symbol": "BTCUSD", "lastPrice": "100"}]

    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: Response())
    try:
        main.fetch_binance_us_shadow_tickers()
    except RuntimeError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("Truncated bulk data should force per-symbol fallback")


def test_coinbase_shadow_prioritizes_setups_and_preserves_mismatched_quote_currency(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    stamp = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(main, "CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN", 1)
    monkeypatch.setattr(main, "CRYPTO_SHADOW_HISTORY_PATH", str(tmp_path / "shadow.json"))
    monkeypatch.setattr(main, "fetch_binance_us_shadow_tickers", lambda: {})
    calls = []

    def fetch_ticker(path, **kwargs):
        calls.append(path)
        return SimpleNamespace(json=lambda: {"price": "100", "time": stamp, "bid": "99.9", "ask": "100.1"})

    monkeypatch.setattr(main, "coinbase_exchange_get", fetch_ticker)
    results = [
        {"symbol": "BTCUSDT", "stage": "LOW-CONVICTION WATCH", "price": 100},
        {"symbol": "ETHUSDT", "stage": "EARLY WATCH", "price": 100},
        {"symbol": "SOLUSDT", "stage": "ACTIONABLE SETUP", "price": 100, "price_quote_currency": "USDT"},
    ]
    report = main.collect_crypto_shadow_comparisons(
        results, run_number=1, coinbase_quotes={"BTC": {"USD"}, "ETH": {"USD"}, "SOL": {"USDC"}},
    )

    assert calls == ["/products/SOL-USDC/ticker"]
    row = next(x for x in report["recent_records"] if x["provider"] == "coinbase")
    assert row["status"] == "quote_currency_mismatch"
    assert row["difference_pct"] is None
    assert row["shadow_quote_currency"] == "USDC"
    assert (row["shadow_bid"], row["shadow_ask"], row["spread_pct"]) == (99.9, 100.1, 0.2)
    assert report["coinbase_execution"]["priority_candidates"] == 2
    assert report["coinbase_execution"]["fresh"] == 1
    assert report["coinbase_execution"]["with_bid_ask"] == 1
