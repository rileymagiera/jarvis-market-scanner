from types import SimpleNamespace

import pytest
import requests

import main


def test_crypto_scan_filters_defaults_and_discovery_before_applying_limits(monkeypatch):
    def product(base, quote="USD", **overrides):
        return {"base_currency": base, "quote_currency": quote, "status": "online", **overrides}

    products = [
        product("SOL"),
        product("ETH", "USDC"),
        product("BTC", limit_only=True),
        {**product("QCAD"), "status": "delisted"},
        product("NOORDERS", trading_disabled=True),
        product("CANCEL", cancel_only=True),
        product("PRELAUNCH", post_only=True),
        product("AUCTION", auction_mode=True),
        product("EURONLY", "EUR"),
    ]
    catalog_requests = []

    def fetch_products(path, **kwargs):
        catalog_requests.append(path)
        return SimpleNamespace(json=lambda: products)

    def ticker(price):
        return {"c": [price], "v": [0, 3_000_000], "p": [0, price], "o": 1}

    monkeypatch.setenv("SCAN_CRYPTO", "QCADUSDT,SOLUSDT,EVAAUSDT,NOORDERSUSDT")
    monkeypatch.setattr(main, "coinbase_exchange_get", fetch_products)
    monkeypatch.setattr(main, "kraken_pairs", lambda: {"EVAA": "evaa", "BTC": "btc", "ETH": "eth"})
    monkeypatch.setattr(main, "KRAKEN_PAIR_QUOTE_MAP", {})
    monkeypatch.setattr(main, "KRAKEN_TICKER_CACHE", None)
    monkeypatch.setattr(main, "KRAKEN_TICKER_CACHE_AT", 0)
    tickers = {"evaa": ticker(100), "btc": ticker(3), "eth": ticker(2)}
    monkeypatch.setattr(main, "kraken_get", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"result": tickers},
    ))
    monkeypatch.setattr(main, "scan_crypto", lambda symbol: {"symbol": symbol, "score": 0})

    assert main.coinbase_spot_quote_currencies() == {"BTC": {"USD"}, "ETH": {"USDC"}, "SOL": {"USD"}}
    assert main.discover_crypto_symbols({"BTC", "ETH", "SOL"}, limit=1) == ["BTCUSDT"]
    catalog_requests.clear()
    result = main.scan_cryptocurrencies(limit=3)
    assert {row["symbol"] for row in result["candidates"]} == {"SOLUSDT", "BTCUSDT", "ETHUSDT"}
    assert catalog_requests == ["/products"]


@pytest.mark.parametrize("payload", [None, [], {"message": "bad response"}])
def test_invalid_or_unavailable_catalog_skips_crypto_candidates(monkeypatch, payload):
    def fetch_products(*args, **kwargs):
        if payload is None:
            raise requests.Timeout("Coinbase timed out")
        return SimpleNamespace(json=lambda: payload)

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("Crypto discovery requires a verified Coinbase catalog")

    monkeypatch.setattr(main, "coinbase_exchange_get", fetch_products)
    monkeypatch.setattr(main, "discover_crypto_symbols", unexpected_discovery)

    result = main.scan_cryptocurrencies(limit=25)
    assert result["candidates"] == []
    assert result["errors"][0]["symbol"] == "crypto_universe"
