import os
import json
import hmac
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, Query, Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Jarvis Market Scanner",
    description="Public-data stock and crypto momentum scanner. Signals are research watchlists, not trade execution.",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

site_security = HTTPBasic()

def require_site_login(credentials: HTTPBasicCredentials = Depends(site_security)):
    expected_user = os.getenv("JARVIS_SITE_USERNAME", "")
    expected_password = os.getenv("JARVIS_SITE_PASSWORD", "")
    valid_user = hmac.compare_digest(credentials.username, expected_user) if expected_user else False
    valid_password = hmac.compare_digest(credentials.password, expected_password) if expected_password else False
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


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

def discover_stock_symbols(limit: int = 70) -> list[str]:
    """Discover US small/mid-cap momentum names across several Yahoo screeners."""
    found = {}
    # The extra screens broaden coverage beyond headline gainers/most-active lists:
    # aggressive_small_caps and most_shorted_stocks can surface earlier-stage movers.
    screens = (
        "small_cap_gainers",
        "aggressive_small_caps",
        "most_shorted_stocks",
        "growth_technology_stocks",
        "day_gainers",
        "most_actives",
    )
    for screen in screens:
        try:
            response = HTTP.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"formatted": "false", "scrIds": screen, "count": 100, "start": 0},
                timeout=15,
            )
            response.raise_for_status()
            results = (response.json().get("finance", {}).get("result") or [])
            quotes = results[0].get("quotes", []) if results else []
            for quote in quotes:
                symbol = str(quote.get("symbol") or "").upper()
                market_cap = safe_float(quote.get("marketCap"))
                price = safe_float(quote.get("regularMarketPrice"))
                volume = safe_float(quote.get("regularMarketVolume"))
                average_volume = safe_float(quote.get("averageDailyVolume3Month"))
                change = safe_float(quote.get("regularMarketChangePercent"))
                if not symbol or market_cap < 10_000_000 or market_cap > 25_000_000_000:
                    continue
                if price < 0.25 or volume < 50_000 or price * volume < 250_000:
                    continue
                relative_volume = volume / average_volume if average_volume else 0
                # Retain modest movers and unusual-volume names; the technical score
                # and grade system decide whether they deserve an early-watch label.
                if change < -3 and relative_volume < 1.5:
                    continue
                rank = change + min(relative_volume, 10) * 2
                found[symbol] = max(rank, found.get(symbol, float("-inf")))
        except Exception:
            # Discovery is additive: a failed screen must not stop the core scan.
            continue
    ranked = sorted(found, key=found.get, reverse=True)
    return ranked[:limit]


KRAKEN = "https://api.kraken.com/0/public"
KRAKEN_PAIR_MAP = None


def crypto_base(symbol: str) -> str:
    value = str(symbol or "").upper()
    for suffix in ("USDT", "USD", "USDC"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return {"XBT": "BTC", "XDG": "DOGE"}.get(value, value)


def kraken_pairs():
    """Map Jarvis base symbols to Kraken's public spot pair identifiers."""
    global KRAKEN_PAIR_MAP
    if KRAKEN_PAIR_MAP is not None:
        return KRAKEN_PAIR_MAP
    response = HTTP.get(f"{KRAKEN}/AssetPairs", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken AssetPairs: " + "; ".join(payload["error"]))
    pairs = {}
    for pair_id, info in (payload.get("result") or {}).items():
        wsname = str(info.get("wsname") or "")
        if "/" not in wsname:
            continue
        base, quote = wsname.split("/", 1)
        if quote not in ("USD", "USDT"):
            continue
        base = crypto_base(base)
        if not base:
            continue
        priority = 0 if quote == "USD" else 1
        current = pairs.get(base)
        if current is None or priority < current[0]:
            pairs[base] = (priority, pair_id)
    KRAKEN_PAIR_MAP = {base: value[1] for base, value in pairs.items()}
    return KRAKEN_PAIR_MAP


def discover_crypto_symbols(limit: int = 25) -> list[str]:
    """Discover liquid early movers from Kraken USD/USDT spot markets."""
    excluded_bases = {"USDC", "FDUSD", "USDT", "TUSD", "USDP", "DAI", "EUR", "TRY", "BRL"}
    leveraged_suffixes = ("UP", "DOWN", "BULL", "BEAR")
    try:
        pairs = kraken_pairs()
        response = HTTP.get(f"{KRAKEN}/Ticker", timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError("Kraken Ticker: " + "; ".join(payload["error"]))
        tickers = payload.get("result") or {}
    except Exception:
        return []
    ranked = []
    for base, pair_id in pairs.items():
        if base in excluded_bases or base.endswith(leveraged_suffixes):
            continue
        ticker = tickers.get(pair_id)
        if not ticker:
            continue
        price = safe_float((ticker.get("c") or [0])[0])
        volume = safe_float((ticker.get("v") or [0, 0])[1])
        vwap = safe_float((ticker.get("p") or [0, 0])[1])
        quote_volume = volume * vwap
        opening = safe_float(ticker.get("o"))
        change = (price / opening - 1) * 100 if opening > 0 else 0
        if price <= 0 or quote_volume < 1_000_000 or change < -10 or change > 150:
            continue
        rank = change * 0.65 + min(np.log10(max(quote_volume, 1)), 9) * 2
        ranked.append((rank, base + "USDT"))
    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:limit]]

def stock_universe() -> list[str]:
    defaults = configured_symbols("SCAN_STOCKS", DEFAULT_STOCKS)
    dynamic = discover_stock_symbols()
    return list(dict.fromkeys(defaults + dynamic))


def crypto_universe() -> list[str]:
    defaults = configured_symbols("SCAN_CRYPTO", DEFAULT_CRYPTO)
    dynamic = discover_crypto_symbols()
    return list(dict.fromkeys(defaults + dynamic))


def safe_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if np.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def market_conditions() -> dict:
    """Broad risk appetite snapshot; missing benchmark data is reported, never guessed."""
    benchmarks = {}
    for symbol in ("SPY", "QQQ", "IWM", "BTC-USD"):
        try:
            hist = yf.Ticker(symbol).history(period="2mo", interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna().astype(float)
            if len(closes) < 21:
                continue
            last = safe_float(closes.iloc[-1])
            ma20 = safe_float(closes.tail(20).mean())
            change5 = (last / safe_float(closes.iloc[-6]) - 1) * 100 if len(closes) >= 6 else 0
            benchmarks[symbol] = {
                "above_20d_average": bool(last > ma20),
                "five_day_change_pct": round(change5, 2),
            }
        except Exception:
            continue
    equity_keys = [k for k in ("SPY", "QQQ", "IWM") if k in benchmarks]
    equity_positive = sum(1 for k in equity_keys if benchmarks[k]["above_20d_average"])
    btc_positive = benchmarks.get("BTC-USD", {}).get("above_20d_average")
    if len(equity_keys) >= 2 and equity_positive >= 2 and btc_positive is not False:
        regime, adjustment = "RISK-ON", 4
    elif len(equity_keys) >= 2 and equity_positive <= 1:
        regime, adjustment = "RISK-OFF", -8
    else:
        regime, adjustment = "MIXED / INCOMPLETE", 0
    return {
        "regime": regime,
        "score_adjustment": adjustment,
        "benchmarks": benchmarks,
        "note": "Benchmark context is a caution/confirmation input, not a standalone buy or sell signal.",
    }


def classify_setup(item: dict, adjustment: int, regime: dict) -> None:
    raw_score = int(item.get("score") or 0)
    adjusted = max(0, min(100, raw_score + adjustment))
    item["raw_score"] = raw_score
    item["score"] = adjusted
    item["market_regime"] = regime["regime"]
    item["market_score_adjustment"] = adjustment
    has_confirmation = (
        float(item.get("relative_volume") or 0) >= 1.5
        or float(item.get("intraday_relative_volume") or 0) >= 2
        or bool(item.get("breakout_20d") or item.get("breakout_24h") or item.get("intraday_breakout"))
    )
    if adjusted >= 75 and has_confirmation and regime["regime"] != "RISK-OFF":
        stage = "ACTIONABLE SETUP"
    elif adjusted >= 55 and has_confirmation:
        stage = "DEVELOPING SETUP"
    elif adjusted >= 35:
        stage = "EARLY WATCH"
    else:
        stage = "LOW-CONVICTION WATCH"
    item["stage"] = stage
    item["signal"] = stage


def levels(price: float, atr: float, support: float) -> dict:
    # Illustrative planning levels only; not personalized trade instructions.
    risk_unit = max(atr, price * 0.02)
    return {
        "watch_entry_low": round(price, 6),
        "watch_entry_high": round(price + risk_unit * 0.25, 6),
        "invalidation_level": round(max(0.0, min(support, price - risk_unit)), 6),
        "method": "ATR/support reference; confirm liquidity and price action before any decision",
    }


SEC_HEADERS = {
    "User-Agent": "JarvisMarketScanner/2.0 (rileymagiera51@gmail.com)",
    "Accept-Encoding": "gzip, deflate",
}
SEC_TICKER_MAP = None


def sec_recent_filings(symbol: str, days: int = 14) -> list[dict]:
    """Fetch recent material SEC filing metadata; filing contents remain for user review."""
    global SEC_TICKER_MAP
    try:
        if SEC_TICKER_MAP is None:
            mapping_response = HTTP.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=15)
            mapping_response.raise_for_status()
            mapping = mapping_response.json()
            SEC_TICKER_MAP = {
                str(item.get("ticker", "")).upper(): str(item.get("cik_str", "")).zfill(10)
                for item in mapping.values()
                if item.get("ticker") and item.get("cik_str")
            }
        cik = SEC_TICKER_MAP.get(symbol.upper())
        if not cik:
            return []
        response = HTTP.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        recent = (response.json().get("filings", {}).get("recent") or {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        documents = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])
        cutoff = datetime.now(timezone.utc).date().toordinal() - days
        material_forms = {"8-K", "S-1", "S-3", "424B1", "424B3", "424B5", "10-Q", "10-K", "13D", "13G"}
        results = []
        for form, filing_date, accession, document, description in zip(forms, dates, accessions, documents, descriptions):
            try:
                filing_ordinal = datetime.strptime(filing_date, "%Y-%m-%d").date().toordinal()
            except (TypeError, ValueError):
                continue
            if form not in material_forms or filing_ordinal < cutoff:
                continue
            accession_path = str(accession).replace("-", "")
            cik_path = str(int(cik))
            results.append({
                "title": f"SEC {form} filed {filing_date}" + (f" — {description}" if description else ""),
                "publisher": "SEC EDGAR",
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_path}/{document}",
                "published_at": filing_date,
                "source_type": "SEC filing",
                "form": form,
            })
            if len(results) >= 3:
                break
        return results
    except Exception:
        # SEC enrichment is additive and must not interrupt price scanning.
        return []


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
        return (headlines[:3] + sec_recent_filings(symbol))[:6]
    except Exception:
        return sec_recent_filings(symbol)



def stock_fundamental_snapshot(ticker) -> dict:
    """Best-effort public company snapshot; missing fields stay null, never inferred."""
    try:
        info = ticker.get_info() or {}
    except Exception:
        return {"status": "unavailable", "note": "Fundamental provider did not return a usable profile."}
    fields = {
        "market_cap": info.get("marketCap"),
        "float_shares": info.get("floatShares"),
        "shares_outstanding": info.get("sharesOutstanding"),
        "short_percent_float": info.get("shortPercentOfFloat"),
        "revenue_growth": info.get("revenueGrowth"),
        "profit_margin": info.get("profitMargins"),
        "operating_cashflow": info.get("operatingCashflow"),
        "total_cash": info.get("totalCash"),
        "total_debt": info.get("totalDebt"),
        "next_earnings_timestamp": info.get("earningsTimestamp"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }
    available = {}
    for key, value in fields.items():
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not np.isfinite(value):
            continue
        if isinstance(value, (str, int, float, bool)):
            available[key] = value
    return {"status": "partial" if available else "unavailable", "fields": available,
            "note": "Yahoo Finance company-profile fields; verify against filings because coverage and freshness vary."}

def scan_stock(symbol: str) -> Optional[dict]:
    ticker = yf.Ticker(symbol)
    frame = ticker.history(period="3mo", interval="1d", auto_adjust=True)
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

    # Intraday bars add earlier momentum and pre/post-market context.
    intraday_change = 0.0
    intraday_relative_volume = 0.0
    intraday_breakout = False
    try:
        intraday = ticker.history(period="5d", interval="15m", prepost=True, auto_adjust=True)
        if not intraday.empty and len(intraday) >= 8:
            intraday_close = intraday["Close"].astype(float).dropna()
            intraday_volume = intraday["Volume"].astype(float).fillna(0)
            intraday_high = intraday["High"].astype(float).dropna()
            if len(intraday_close) >= 5:
                intraday_change = (safe_float(intraday_close.iloc[-1]) / safe_float(intraday_close.iloc[-5]) - 1) * 100
            baseline_volume = safe_float(intraday_volume.iloc[-21:-1].median()) if len(intraday_volume) >= 21 else 0
            current_intraday_volume = safe_float(intraday_volume.iloc[-1])
            intraday_relative_volume = current_intraday_volume / baseline_volume if baseline_volume > 0 else 0
            if len(intraday_high) >= 21:
                intraday_breakout = safe_float(intraday_close.iloc[-1]) > safe_float(intraday_high.iloc[-21:-1].max())
    except Exception:
        # Keep the daily scan usable if intraday data is unavailable.
        pass

    score = 0
    score += 15 if daily_change >= 2 else 8 if daily_change >= 0.5 else 0
    score += 15 if five_day_change >= 5 else 8 if five_day_change >= 2 else 0
    score += 20 if relative_volume >= 2.5 else 14 if relative_volume >= 1.8 else 7 if relative_volume >= 1.3 else 0
    score += 20 if breakout else 0
    score += 15 if trend_up else 0
    score += 15 if daily_change > 0 and relative_volume >= 1.5 else 0
    score += 10 if intraday_change >= 2 else 5 if intraday_change >= 0.75 else 0
    score += 10 if intraday_relative_volume >= 3 else 5 if intraday_relative_volume >= 2 else 0
    score += 5 if intraday_breakout else 0
    score = min(score, 100)

    if score < 15:
        return None
    news = stock_news(symbol)
    fundamentals = stock_fundamental_snapshot(ticker) if score >= 35 else {"status": "not_fetched_for_low_score"}
    risk_flags = []
    if any(item.get("form") in {"S-1", "S-3", "424B1", "424B3", "424B5"} for item in news):
        risk_flags.append("Potential offering/registration filing detected; review the SEC document")
    return {
        "symbol": symbol,
        "asset_type": "stock",
        "price": round(price, 4),
        "daily_change_pct": round(daily_change, 2),
        "five_day_change_pct": round(five_day_change, 2),
        "relative_volume": round(relative_volume, 2),
        "intraday_change_pct": round(intraday_change, 2),
        "intraday_relative_volume": round(intraday_relative_volume, 2),
        "intraday_breakout": bool(intraday_breakout),
        "breakout_20d": bool(breakout),
        "above_20d_average": bool(trend_up),
        "score": score,
        "signal": "HIGH INTEREST" if score >= 75 else "WATCH" if score >= 50 else "EARLY WATCH",
        "levels": levels(price, atr, support),
        "news": news,
        "risk_flags": risk_flags,
        "fundamentals": fundamentals,
        "research_notes": [
            "Review latest SEC filings for financing, dilution, debt and material events.",
            "Confirm headline timing and catalyst with a second independent source before acting.",
            "Check float, shares outstanding, cash runway and upcoming earnings; these are not fully verified by this scan.",
        ],
        "data_source": "Yahoo Finance daily + 15m intraday (pre/post-market when available)",
    }


def crypto_news(symbol: str) -> list[dict]:
    try:
        yahoo_symbol = symbol[:-4] + "-USD" if symbol.endswith("USDT") else symbol
        items = yf.Ticker(yahoo_symbol).news or []
        headlines = []
        for item in items[:3]:
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
        return headlines
    except Exception:
        return []


def coinbase_crypto_market_data(symbol: str):
    """Fallback public market data from Coinbase Exchange; no API key required."""
    base = crypto_base(symbol)
    product = f"{base}-USD"
    ticker_response = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=12)
    ticker_response.raise_for_status()
    ticker = ticker_response.json()
    stats_response = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/stats", timeout=12)
    stats_response.raise_for_status()
    stats = stats_response.json()
    candles_response = HTTP.get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": 3600},
        timeout=15,
    )
    candles_response.raise_for_status()
    # Coinbase returns [time, low, high, open, close, volume], newest first.
    raw = list(reversed(candles_response.json()))
    bars = [[row[0], row[3], row[2], row[1], row[4], row[5]] for row in raw if len(row) >= 6]
    price = safe_float(ticker.get("price"))
    opening = safe_float(stats.get("open"))
    volume = safe_float(stats.get("volume"))
    if price <= 0 or opening <= 0:
        raise RuntimeError(f"Coinbase returned incomplete market data for {product}")
    normalized = {
        "lastPrice": price,
        "priceChangePercent": (price / opening - 1) * 100,
        "quoteVolume": volume * price,
    }
    return normalized, bars, "Coinbase Exchange public API"


def kraken_crypto_market_data(symbol: str):
    """Primary public market data from Kraken Spot REST API."""
    base = crypto_base(symbol)
    pair_id = kraken_pairs().get(base)
    if not pair_id:
        raise RuntimeError(f"Kraken has no supported USD/USDT spot pair for {base}")
    ticker_response = HTTP.get(f"{KRAKEN}/Ticker", params={"pair": pair_id}, timeout=12)
    ticker_response.raise_for_status()
    ticker_payload = ticker_response.json()
    if ticker_payload.get("error"):
        raise RuntimeError("Kraken Ticker: " + "; ".join(ticker_payload["error"]))
    ticker_rows = ticker_payload.get("result") or {}
    if not ticker_rows:
        raise RuntimeError(f"Kraken returned no ticker for {base}")
    ticker = next(iter(ticker_rows.values()))
    price = safe_float((ticker.get("c") or [0])[0])
    opening = safe_float(ticker.get("o"))
    volume = safe_float((ticker.get("v") or [0, 0])[1])
    vwap = safe_float((ticker.get("p") or [0, 0])[1])
    if price <= 0 or opening <= 0:
        raise RuntimeError(f"Kraken returned incomplete ticker for {base}")
    bars_response = HTTP.get(
        f"{KRAKEN}/OHLC",
        params={"pair": pair_id, "interval": 60},
        timeout=12,
    )
    bars_response.raise_for_status()
    bars_payload = bars_response.json()
    if bars_payload.get("error"):
        raise RuntimeError("Kraken OHLC: " + "; ".join(bars_payload["error"]))
    bars_rows = [value for key, value in (bars_payload.get("result") or {}).items() if key != "last"]
    raw = bars_rows[0] if bars_rows else []
    # Kraken returns [time, open, high, low, close, vwap, volume, count].
    bars = [[row[0], row[1], row[2], row[3], row[4], row[6]] for row in raw if len(row) >= 8]
    normalized = {
        "lastPrice": price,
        "priceChangePercent": (price / opening - 1) * 100,
        "quoteVolume": volume * vwap,
    }
    return normalized, bars, "Kraken Spot public API"


def scan_crypto(symbol: str) -> Optional[dict]:
    try:
        ticker, bars, provider = kraken_crypto_market_data(symbol)
    except Exception as primary_error:
        try:
            ticker, bars, provider = coinbase_crypto_market_data(symbol)
        except Exception as backup_error:
            raise RuntimeError(
                f"Kraken primary failed ({primary_error}); Coinbase backup failed ({backup_error})"
            ) from backup_error
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
    if score < 15:
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
        "news": crypto_news(symbol),
        "data_source": f"{provider} + Yahoo Finance headlines",
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


@app.get("/", dependencies=[Depends(require_site_login)])
def home():
    return {
        "name": "Jarvis Market Scanner",
        "status": "online",
        "version": "2.0.0",
        "features": ["stock momentum", "crypto momentum", "relative volume", "breakout detection", "news headlines", "illustrative risk levels"],
        "endpoints": ["/health", "/scan", "/scan/stocks", "/scan/crypto", "/symbols"],
    }



PAPER_STATE = Path("paper_tracker.json")
PAPER_LIFETIME_STATE = Path("paper_tracker_lifetime.json")
HISTORY_SNAPSHOT = Path("history_snapshot.json")


@app.get("/history", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def history_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis History</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font:15px system-ui,sans-serif;margin:0;background:#0d131b;color:#edf2f7}main{max-width:1200px;margin:auto;padding:20px}h1{margin:0 0 5px}.muted,.tiny{color:#9eacbd}.tiny{font-size:12px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}.card,.panel{background:#172331;border:1px solid #2b3b4d;border-radius:12px;padding:14px}.value{font-size:24px;font-weight:750}.section{margin-top:26px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}table{border-collapse:collapse;width:100%;min-width:700px}th,td{text-align:left;padding:9px;border-bottom:1px solid #2b3b4d;font-size:13px}th{color:#aebaca}.scroll{overflow-x:auto;margin-top:10px}.pos{color:#62d99a}.neg{color:#ff8989}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}input,select{background:#0d1722;color:#edf2f7;border:1px solid #40536a;border-radius:8px;padding:9px;font:inherit}input{flex:1;min-width:160px}.pill{border:1px solid #40536a;border-radius:99px;padding:3px 8px;font-size:11px}.empty{text-align:center;padding:16px;color:#9eacbd}@media(max-width:600px){main{padding:14px}.value{font-size:21px}}
</style></head><body><main><h1>Jarvis History & Performance</h1><div class="muted" id="updated">Loading saved tracking history…</div><div class="tiny">Research tracking only. Returns are measured from first detection, not trade fills. Jarvis never places trades.</div>
<div class="cards" id="stats"></div>
<section class="section"><h2>Stock vs. crypto scoreboard</h2><div class="grid" id="assets"></div></section>
<section class="section"><h2>Performance leaderboard</h2><div class="muted">Top tracked candidates by return from first tracked price.</div><div class="scroll"><table><thead><tr><th>#</th><th>Candidate</th><th>First price</th><th>Latest</th><th>Return</th><th>Best</th><th>Worst</th></tr></thead><tbody id="leaders"></tbody></table></div></section>
<section class="section"><h2>All tracked candidates</h2><div class="toolbar"><input id="q" placeholder="Search ticker…"><select id="assetFilter"><option value="all">All assets</option><option value="stock">Stocks</option><option value="crypto">Crypto</option></select><select id="gradeFilter"><option value="all">All grades</option><option>ACTIONABLE SETUP</option><option>DEVELOPING SETUP</option><option>EARLY WATCH</option><option>LOW-CONVICTION WATCH</option></select></div><div class="scroll"><table><thead><tr><th>Candidate</th><th>First seen</th><th>First price</th><th>Current grade</th><th>Return</th><th>Best</th><th>Worst</th><th>Age</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="section"><h2>Accuracy scorecard</h2><div class="muted">Current observed outcomes; not a guarantee of future performance.</div><div class="grid"><div class="panel"><h3>Return thresholds</h3><div id="thresholds"></div></div><div class="panel"><h3>Lifetime checkpoints</h3><div class="scroll"><table><thead><tr><th>Days</th><th>Observations</th><th>Wins</th><th>Losses</th><th>Average return</th></tr></thead><tbody id="checks"></tbody></table></div></div></div></section>
<section class="section"><h2>Grade movement history</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>From</th><th>To</th><th>When</th></tr></thead><tbody id="moves"></tbody></table></div></section>
<section class="section"><h2>Discovery reasons & catalysts</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>Signals</th><th>Latest headline / filing</th><th>Risk flags</th></tr></thead><tbody id="catalysts"></tbody></table></div></section>
<section class="section"><h2>Not in latest scan</h2><div class="tiny">Previously tracked, but absent from the latest scan. Last recorded prices may be stale; this is not a sell signal.</div><div class="scroll"><table><thead><tr><th>Candidate</th><th>Last seen</th><th>Last return</th><th>Best gain</th><th>Last grade</th></tr></thead><tbody id="faded"></tbody></table></div></section>
<p class="tiny section">Data may be delayed or incomplete. Candidate discovery is not the same as an actionable alert.</p></main><script>
const n=v=>Number(v||0),pct=v=>n(v).toFixed(2)+'%',cl=v=>n(v)>0?'pos':n(v)<0?'neg':'',e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"),date=v=>v?new Date(v).toLocaleString():'—';
const rank={'ACTIONABLE SETUP':4,'DEVELOPING SETUP':3,'EARLY WATCH':2,'LOW-CONVICTION WATCH':1};
function render(d){const t=d.paper_tracker||{},l=d.lifetime||{},all=Object.values(t),live=all.filter(x=>x.currently_surfaced!==false),avg=all.length?all.reduce((s,x)=>s+n(x.return_pct),0)/all.length:0;
const fmtTime=d.synced_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',dateStyle:'medium',timeStyle:'medium',timeZoneName:'short'}).format(new Date(d.synced_at)):'Not yet synced';document.getElementById('updated').textContent='Last synced: '+fmtTime;
const thresholds=[['Positive now',all.filter(x=>n(x.return_pct)>0).length],['At least +5%',all.filter(x=>n(x.return_pct)>=5).length],['At least +10%',all.filter(x=>n(x.return_pct)>=10).length],['At least +20%',all.filter(x=>n(x.return_pct)>=20).length],['Down 5% or more',all.filter(x=>n(x.return_pct)<=-5).length],['Ever reached +10%',all.filter(x=>n(x.max_gain_pct)>=10).length]];
document.getElementById('stats').innerHTML=[['Tracked candidates',all.length],['In latest scan',live.length],['Average return',pct(avg)],['Positive candidates',thresholds[0][1]+' / '+all.length],['Lifetime checkpoint wins',Object.values(l.checkpoints||{}).reduce((s,x)=>s+n(x.wins),0)],['Top current return',pct(Math.max(0,...all.map(x=>n(x.return_pct))))]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');
document.getElementById('assets').innerHTML=['stock','crypto'].map(k=>{const a=all.filter(x=>x.asset_type===k),m=a.length?a.reduce((s,x)=>s+n(x.return_pct),0)/a.length:0;return '<div class="panel"><div class="muted">'+(k==='stock'?'Stocks':'Crypto')+'</div><div class="value">'+a.length+' tracked</div><div class="'+cl(m)+'">Average return '+pct(m)+'</div><div class="tiny">'+a.filter(x=>n(x.return_pct)>0).length+' positive · '+a.filter(x=>x.last_stage==='ACTIONABLE SETUP').length+' actionable grade</div></div>'}).join('');
const byReturn=[...all].sort((a,b)=>n(b.return_pct)-n(a.return_pct));document.getElementById('leaders').innerHTML=byReturn.slice(0,10).map((x,i)=>'<tr><td>'+(i+1)+'</td><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td>'+n(x.latest_price).toPrecision(7)+'</td><td class="'+cl(x.return_pct)+'">'+pct(x.return_pct)+'</td><td class="pos">'+pct(x.max_gain_pct)+'</td><td class="neg">'+pct(x.max_drawdown_pct)+'</td></tr>').join('')||'<tr><td colspan="7" class="empty">No tracked candidates yet.</td></tr>';
function rows(){const q=document.getElementById('q').value.toUpperCase(),a=document.getElementById('assetFilter').value,g=document.getElementById('gradeFilter').value;const v=all.filter(x=>(a==='all'||x.asset_type===a)&&(g==='all'||x.last_stage===g)&&(!q||String(x.symbol).toUpperCase().includes(q)));v.sort((x,y)=>(rank[y.last_stage]||0)-(rank[x.last_stage]||0)||(y.last_seen||'').localeCompare(x.last_seen||''));document.getElementById('rows').innerHTML=v.map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.first_seen)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td><span class="pill">'+e(x.last_stage||'—')+'</span></td><td class="'+cl(x.return_pct)+'">'+pct(x.return_pct)+'</td><td class="pos">'+pct(x.max_gain_pct)+'</td><td class="neg">'+pct(x.max_drawdown_pct)+'</td><td>'+n(x.age_days).toFixed(1)+'d</td></tr>').join('')||'<tr><td colspan="8" class="empty">No matches.</td></tr>'}
['q','assetFilter','gradeFilter'].forEach(id=>document.getElementById(id).addEventListener(id==='q'?'input':'change',rows));rows();
document.getElementById('thresholds').innerHTML=thresholds.map(x=>'<div style="display:flex;justify-content:space-between;padding:7px;border-bottom:1px solid #2b3b4d"><span>'+e(x[0])+'</span><b>'+x[1]+'</b></div>').join('');
const cps=l.checkpoints||{};document.getElementById('checks').innerHTML=Object.entries(cps).sort((a,b)=>n(a[0])-n(b[0])).map(([h,x])=>'<tr><td>'+e(h)+'</td><td>'+n(x.observations)+'</td><td>'+n(x.wins)+'</td><td>'+n(x.losses)+'</td><td>'+pct(x.observations?n(x.total_return_pct)/n(x.observations):0)+'</td></tr>').join('')||'<tr><td colspan="5">Checkpoints populate as records mature.</td></tr>';
const moves=[];all.forEach(x=>(x.grade_history||[]).forEach(m=>moves.push({...m,symbol:x.symbol,asset:x.asset_type})));moves.sort((a,b)=>new Date(b.at)-new Date(a.at));document.getElementById('moves').innerHTML=moves.slice(0,50).map(m=>'<tr><td>'+e((m.asset||'').toUpperCase()+' '+m.symbol)+'</td><td>'+e(m.from||'—')+'</td><td>'+e(m.to||'—')+'</td><td>'+date(m.at)+'</td></tr>').join('')||'<tr><td colspan="4">Grade history starts recording from this upgrade onward.</td></tr>';
document.getElementById('catalysts').innerHTML=all.filter(x=>(x.discovery_reasons||[]).length||(x.catalysts||[]).length||(x.risk_flags||[]).length).slice(0,40).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e((x.discovery_reasons||[]).join(' · ')||'—')+'</td><td>'+((x.catalysts||[]).slice(0,2).map(c=>c.url?'<a target="_blank" rel="noopener" href="'+e(c.url)+'">'+e(c.title)+'</a>':e(c.title)).join('<br>')||'—')+'</td><td class="neg">'+e((x.risk_flags||[]).join(' · ')||'—')+'</td></tr>').join('')||'<tr><td colspan="4">Discovery details will populate on the next scan.</td></tr>';
const faded=all.filter(x=>x.currently_surfaced===false).sort((a,b)=>(b.last_seen||'').localeCompare(a.last_seen||''));document.getElementById('faded').innerHTML=faded.slice(0,40).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.last_seen)+'</td><td class="'+cl(x.return_pct)+'">'+pct(x.return_pct)+'</td><td class="pos">'+pct(x.max_gain_pct)+'</td><td>'+e(x.last_stage||'—')+'</td></tr>').join('')||'<tr><td colspan="5">No candidates currently absent from the latest scan.</td></tr>';
}
fetch('/history/data').then(r=>{if(!r.ok)throw Error();return r.json()}).then(render).catch(()=>document.getElementById('updated').textContent='History temporarily unavailable; try again after the next sync.');
</script></body></html>""")
 
 
@app.get("/history/data", dependencies=[Depends(require_site_login)])
def history_data():
    def read_state(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # Prefer the latest scan snapshot pushed by GitHub Actions. Fall back to
    # the checked-in tracker files so history remains visible before first sync.
    snapshot = read_state(HISTORY_SNAPSHOT)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("paper_tracker"), dict):
        return {
            "synced_at": snapshot.get("synced_at"),
            "paper_tracker": snapshot.get("paper_tracker", {}),
            "lifetime": snapshot.get("lifetime", {}),
        }
    paper = read_state(PAPER_STATE)
    lifetime = read_state(PAPER_LIFETIME_STATE)
    timestamps = [
        path.stat().st_mtime
        for path in (PAPER_STATE, PAPER_LIFETIME_STATE)
        if path.exists()
    ]
    synced_at = datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat() if timestamps else None
    return {"synced_at": synced_at, "paper_tracker": paper, "lifetime": lifetime}


@app.post("/history/sync")
def history_sync(payload: dict, x_jarvis_history_token: Optional[str] = Header(None)):
    expected = os.getenv("JARVIS_HISTORY_SYNC_TOKEN", "")
    if not expected or not x_jarvis_history_token or not hmac.compare_digest(expected, x_jarvis_history_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not isinstance(payload.get("paper_tracker"), dict) or not isinstance(payload.get("lifetime"), dict):
        raise HTTPException(status_code=400, detail="Invalid history payload")
    HISTORY_SNAPSHOT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {"status": "synced", "records": len(payload["paper_tracker"])}


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": now_iso()}


@app.get("/symbols", dependencies=[Depends(require_site_login)])
def symbols():
    return {
        "stocks": stock_universe(),
        "crypto": crypto_universe(),
    }


@app.get("/scan/stocks", dependencies=[Depends(require_site_login)])
def scan_stocks(limit: int = Query(30, ge=1, le=100)):
    universe = stock_universe()[:limit]
    results, errors = run_parallel(universe, scan_stock)
    return response_payload("stocks", universe, results, errors)


@app.get("/scan/crypto", dependencies=[Depends(require_site_login)])
def scan_cryptocurrencies(limit: int = Query(25, ge=1, le=100)):
    universe = crypto_universe()[:limit]
    results, errors = run_parallel(universe, scan_crypto)
    return response_payload("crypto", universe, results, errors)


@app.get("/scan", dependencies=[Depends(require_site_login)])
def scan(asset: str = Query("all", pattern="^(all|stocks|crypto)$")):
    stock_symbols = stock_universe()
    crypto_symbols = crypto_universe()
    stock_results, stock_errors = ([], [])
    crypto_results, crypto_errors = ([], [])
    if asset in ("all", "stocks"):
        stock_results, stock_errors = run_parallel(stock_symbols, scan_stock)
    if asset in ("all", "crypto"):
        crypto_results, crypto_errors = run_parallel(crypto_symbols, scan_crypto)
    results = sorted(stock_results + crypto_results, key=lambda item: item["score"], reverse=True)
    errors = stock_errors + crypto_errors
    regime = market_conditions()
    for item in results:
        classify_setup(item, regime["score_adjustment"], regime)
    results.sort(key=lambda item: item["score"], reverse=True)
    return {
        **response_payload(asset, stock_symbols + crypto_symbols, results, errors),
        "market_conditions": regime,
        "stock_candidates": len(stock_results),
        "crypto_candidates": len(crypto_results),
        "diagnostics": {
            "stocks": {
                "symbols_requested": len(stock_symbols),
                "candidates_returned": len(stock_results),
                "scan_errors": len(stock_errors),
                "not_returned_or_below_early_watch_floor": max(0, len(stock_symbols) - len(stock_results) - len(stock_errors)),
                "stages": {stage: sum(1 for x in stock_results if x.get("stage") == stage) for stage in ("ACTIONABLE SETUP", "DEVELOPING SETUP", "EARLY WATCH", "LOW-CONVICTION WATCH")},
            },
            "crypto": {
                "symbols_requested": len(crypto_symbols),
                "candidates_returned": len(crypto_results),
                "scan_errors": len(crypto_errors),
                "not_returned_or_below_early_watch_floor": max(0, len(crypto_symbols) - len(crypto_results) - len(crypto_errors)),
                "stages": {stage: sum(1 for x in crypto_results if x.get("stage") == stage) for stage in ("ACTIONABLE SETUP", "DEVELOPING SETUP", "EARLY WATCH", "LOW-CONVICTION WATCH")},
            },
            "provider_note": "Counts show requested symbols and scan outcomes. A symbol not returned may have insufficient history/data or score below the early-watch floor; provider-specific discovery failures are not always attributable to one symbol.",
        },
    }
