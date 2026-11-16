import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Jarvis Market Scanner",
    description="Public-data stock and crypto momentum scanner. Signals are research watchlists, not trade execution.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DEFAULT_STOCKS = [
    "PLTR", "RKLB", "ASTS", "SOUN", "BBAI", "IONQ", "RGTI", "QBTS",
    "HIMS", "ONDS", "LUNR", "RDW", "ACHR", "JOBY", "SMCI", "NVDA",
    "AMD", "TSLA", "MARA", "RIOT", "CLSK", "IREN", "HOOD", "COIN",
    "SOFI", "OPEN", "SERV", "TEM", "OKLO", "SMR",
]
DEFAULT_CRYPTO = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "SUIUSDT", "SEIUSDT", "APTUSDT", "INJUSDT", "NEARUSDT",
    "RENDERUSDT", "FETUSDT", "TAOUSDT", "WIFUSDT", "PEPEUSDT",
    "BONKUSDT", "JUPUSDT", "ONDOUSDT", "ARBUSDT", "OPUSDT",
]
BINANCE = "https://api.binance.com"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "JarvisMarketScanner/2.0"})


def configured_symbols(env_name: str, defaults: list[str]) -> list[str]:
    raw = os.getenv(env_name, "").strip()
    values = raw.split(",") if raw else defaults
    return list(dict.fromkeys(v.strip().upper() for v in values if v.strip()))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if np.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def levels(price: float, atr: float, support: float) -> dict:
    # Illustrative planning levels only; not personalized trade instructions.
    risk_unit = max(atr, price * 0.02)
    return {
        "watch_entry_low": round(price, 6),
        "watch_entry_high": round(price + risk_unit * 0.25, 6),
        "invalidation_level": round(max(0.0, min(support, price - risk_unit)), 6),
        "method": "ATR/support reference; confirm liquidity and price action before any decision",
    }


def stock_news(symbol: str) -> list[dict]:
    try:
        items = yf.Ticker(symbol).news or []
        headlines = []
        for item in items[:5]:
            node = item.get("content", item)
            title = node.get("title") or item.get("title")
            if not title:
                continue
            provider = node.get("provider") or {}
            canonical = node.get("canonicalUrl") or {}
            headlines.append({
                "title": title,
                "publisher": provider.get("displayName") if isinstance(provider, dict) else None,
                "url": canonical.get("url") if isinstance(canonical, dict) else None,
                "published_at": node.get("pubDate"),
            })
        return headlines[:3]
    except Exception:
        return []


def scan_stock(symbol: str) -> Optional[dict]:
    frame = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=True)
    if frame.empty or len(frame) < 22:
        return None
    frame = frame.dropna(subset=["Close", "Volume"])
    if len(frame) < 22:
        return None

    close = frame["Close"].astype(float)
    volume = frame["Volume"].astype(float)
    price = safe_float(close.iloc[-1])
    previous = safe_float(close.iloc[-2])
    avg_volume = safe_float(volume.iloc[-21:-1].mean())
    current_volume = safe_float(volume.iloc[-1])
    if price <= 0 or previous <= 0 or avg_volume <= 0:
        return None

    daily_change = (price / previous - 1) * 100
    five_day_change = (price / safe_float(close.iloc[-6], price) - 1) * 100
    relative_volume = current_volume / avg_volume
    prior_high = safe_float(close.iloc[-21:-1].max())
    breakout = price > prior_high
    ma20 = safe_float(close.tail(20).mean())
    trend_up = price > ma20
    high_low = frame["High"].astype(float) - frame["Low"].astype(float)
    atr = safe_float(high_low.tail(14).mean(), price * 0.03)
    support = safe_float(frame["Low"].tail(10).min(), price * 0.95)

    score = 0
    score += 15 if daily_change >= 2 else 8 if daily_change >= 0.5 else 0
    score += 15 if five_day_change >= 5 else 8 if five_day_change >= 2 else 0
    score += 20 if relative_volume >= 2.5 else 14 if relative_volume >= 1.8 else 7 if relative_volume >= 1.3 else 0
    score += 20 if breakout else 0
    score += 15 if trend_up else 0
    score += 15 if daily_change > 0 and relative_volume >= 1.5 else 0

    if score < 25:
        return None
    return {
        "symbol": symbol,
        "asset_type": "stock",
        "price": round(price, 4),
        "daily_change_pct": round(daily_change, 2),
        "five_day_change_pct": round(five_day_change, 2),
        "relative_volume": round(relative_volume, 2),
        "breakout_20d": bool(breakout),
        "above_20d_average": bool(trend_up),
        "score": score,
        "signal": "HIGH INTEREST" if score >= 75 else "WATCH" if score >= 50 else "EARLY WATCH",
        "levels": levels(price, atr, support),
        "news": stock_news(symbol),
        "data_source": "Yahoo Finance",
    }


def scan_crypto(symbol: str) -> Optional[dict]:
    ticker_response = HTTP.get(
        f"{BINANCE}/api/v3/ticker/24hr",
        params={"symbol": symbol},
        timeout=12,
    )
    ticker_response.raise_for_status()
    ticker = ticker_response.json()
    bars_response = HTTP.get(
        f"{BINANCE}/api/v3/klines",
        params={"symbol": symbol, "interval": "1h", "limit": 50},
        timeout=12,
    )
    bars_response.raise_for_status()
    bars = bars_response.json()
    if len(bars) < 26:
        return None

    # Exclude the currently-forming hourly candle from volume comparisons.
    closed = bars[:-1]
    closes = [safe_float(row[4]) for row in closed]
    highs = [safe_float(row[2]) for row in closed]
    lows = [safe_float(row[3]) for row in closed]
    volumes = [safe_float(row[5]) for row in closed]
    price = safe_float(ticker.get("lastPrice"))
    if price <= 0:
        return None

    change_24h = safe_float(ticker.get("priceChangePercent"))
    change_1h = (closes[-1] / closes[-2] - 1) * 100 if closes[-2] else 0
    baseline = float(np.mean(volumes[-25:-1])) if len(volumes) >= 25 else 0
    relative_volume = volumes[-1] / baseline if baseline > 0 else 0
    prior_high = max(highs[-25:-1])
    breakout = closes[-1] > prior_high
    ma24 = float(np.mean(closes[-24:]))
    trend_up = closes[-1] > ma24
    atr = float(np.mean([highs[i] - lows[i] for i in range(-14, 0)]))
    support = min(lows[-12:])

    score = 0
    score += 15 if change_1h >= 1.5 else 8 if change_1h >= 0.5 else 0
    score += 15 if change_24h >= 5 else 8 if change_24h >= 2 else 0
    score += 20 if relative_volume >= 2.5 else 14 if relative_volume >= 1.8 else 7 if relative_volume >= 1.3 else 0
    score += 20 if breakout else 0
    score += 15 if trend_up else 0
    score += 15 if change_1h > 0 and relative_volume >= 1.5 else 0
    if score < 25:
        return None

    return {
        "symbol": symbol,
        "asset_type": "crypto",
        "price": price,
        "daily_change_pct": round(change_24h, 2),
        "hourly_change_pct": round(change_1h, 2),
        "relative_volume": round(relative_volume, 2),
        "breakout_24h": bool(breakout),
        "above_24h_average": bool(trend_up),
        "quote_volume_24h": round(safe_float(ticker.get("quoteVolume")), 2),
        "score": score,
        "signal": "HIGH INTEREST" if score >= 75 else "WATCH" if score >= 50 else "EARLY WATCH",
        "levels": levels(price, atr, support),
        "news": [],
        "data_source": "Binance public market data",
    }


def run_parallel(symbols: list[str], scanner, workers: int = 6) -> tuple[list, list]:
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scanner, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                item = future.result()
                if item:
                    results.append(item)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)[:240]})
    results.sort(key=lambda item: item["score"], reverse=True)
    return results, errors


def response_payload(asset: str, symbols: list[str], results: list, errors: list) -> dict:
    return {
        "scanner": "Jarvis",
        "version": "2.0.0",
        "asset": asset,
        "timestamp": now_iso(),
        "symbols_requested": len(symbols),
        "candidates_found": len(results),
        "candidates": results,
        "errors": errors,
        "notice": "Research watchlist only. Data may be delayed or incomplete. Not financial advice; no trades are placed.",
    }


@app.get("/")
def home():
    return {
        "name": "Jarvis Market Scanner",
        "status": "online",
        "version": "2.0.0",
        "features": ["stock momentum", "crypto momentum", "relative volume", "breakout detection", "news headlines", "illustrative risk levels"],
        "endpoints": ["/health", "/scan", "/scan/stocks", "/scan/crypto", "/symbols"],
    }


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": now_iso()}


@app.get("/symbols")
def symbols():
    return {
        "stocks": configured_symbols("SCAN_STOCKS", DEFAULT_STOCKS),
        "crypto": configured_symbols("SCAN_CRYPTO", DEFAULT_CRYPTO),
    }


@app.get("/scan/stocks")
def scan_stocks(limit: int = Query(30, ge=1, le=100)):
    universe = configured_symbols("SCAN_STOCKS", DEFAULT_STOCKS)[:limit]
    results, errors = run_parallel(universe, scan_stock)
    return response_payload("stocks", universe, results, errors)


@app.get("/scan/crypto")
def scan_cryptocurrencies(limit: int = Query(25, ge=1, le=100)):
    universe = configured_symbols("SCAN_CRYPTO", DEFAULT_CRYPTO)[:limit]
    results, errors = run_parallel(universe, scan_crypto)
    return response_payload("crypto", universe, results, errors)


@app.get("/scan")
def scan(asset: str = Query("all", pattern="^(all|stocks|crypto)$")):
    stock_symbols = configured_symbols("SCAN_STOCKS", DEFAULT_STOCKS)
    crypto_symbols = configured_symbols("SCAN_CRYPTO", DEFAULT_CRYPTO)
    stock_results, stock_errors = ([], [])
    crypto_results, crypto_errors = ([], [])
    if asset in ("all", "stocks"):
        stock_results, stock_errors = run_parallel(stock_symbols, scan_stock)
    if asset in ("all", "crypto"):
        crypto_results, crypto_errors = run_parallel(crypto_symbols, scan_crypto)
    results = sorted(stock_results + crypto_results, key=lambda item: item["score"], reverse=True)
    errors = stock_errors + crypto_errors
    return {
        **response_payload(asset, stock_symbols + crypto_symbols, results, errors),
        "stock_candidates": len(stock_results),
        "crypto_candidates": len(crypto_results),
    }
