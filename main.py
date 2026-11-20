import os
import gzip
import json
import hmac
import re
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import numpy as np
import pandas as pd
import requests
from io import BytesIO, StringIO
import yfinance as yf
from early_runner import compute_early_runner_metrics, compute_early_warning_metrics, compute_stock_market_breadth, rank_stock_universe_strength, summarize_crypto_cross_section, set_crypto_relative_strength_coverage, build_data_quality_record, apply_entry_quality_gate, has_session_aware_early_runner_baseline
from market_calendar import market_session_status as exchange_market_session_status
from market_data_cache import HourlyCandleCache
from runner_intelligence import build_runner_intelligence_report
from historical_audit_export import build_historical_audit_export
from buy_intelligence import build_buy_intelligence, blend_buy_score
from paper_trading import load_account as load_paper_account, account_summary as summarize_paper_account
from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, Response, RedirectResponse
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


def market_session_status(now=None) -> dict:
    """Return the XNYS calendar-aware equity session and extended-hours phase."""
    return exchange_market_session_status(now)

def discover_stock_symbols(limit: int = 120) -> list[str]:
    """Discover a broad stock universe while keeping the existing quality pool first.

    The strict pool preserves the previous discovery rules. A separate, lower-
    threshold early-candidate pool widens research coverage; it does not change
    technical scoring, stage assignment, or BUY confirmation.
    """
    strict_found = {}
    early_found = {}
    screens = (
        "small_cap_gainers",
        "aggressive_small_caps",
        "most_shorted_stocks",
        "growth_technology_stocks",
        "day_gainers",
        "most_actives",
    )

    def fetch_screen(screen: str) -> list[dict]:
        started = time.monotonic()
        try:
            response = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"formatted": "false", "scrIds": screen, "count": 150, "start": 0},
                headers={"User-Agent": "JarvisMarketScanner/2.0"},
                timeout=12,
            )
            response.raise_for_status()
            results = (response.json().get("finance", {}).get("result") or [])
            record_scan_metric("yahoo_discovery_screen", time.monotonic() - started)
            return results[0].get("quotes", []) if results else []
        except Exception as exc:
            record_scan_metric("yahoo_discovery_screen", time.monotonic() - started, "failure")
            print(f"Yahoo discovery screen {screen} failed: {type(exc).__name__}")
            return []

    with ThreadPoolExecutor(max_workers=len(screens)) as pool:
        screen_quotes = list(pool.map(fetch_screen, screens))

    for quotes in screen_quotes:
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
                continue
            market_cap = safe_float(quote.get("marketCap"))
            price = safe_float(quote.get("regularMarketPrice"))
            volume = safe_float(quote.get("regularMarketVolume"))
            average_volume = safe_float(quote.get("averageDailyVolume3Month"))
            change = safe_float(quote.get("regularMarketChangePercent"))
            if market_cap <= 0 or price <= 0 or volume <= 0:
                continue
            relative_volume = volume / average_volume if average_volume > 0 else 0.0
            rank = change + min(relative_volume, 12) * 2

            # Original, stricter discovery lane. Preserve these candidates and
            # their ordering ahead of the expanded early-research lane.
            if (
                10_000_000 <= market_cap <= 25_000_000_000
                and price >= 0.25
                and volume >= 50_000
                and price * volume >= 250_000
                and not (change < -3 and relative_volume < 1.5)
            ):
                strict_found[symbol] = max(rank, strict_found.get(symbol, float("-inf")))

            # Broader research-only lane: admits smaller/cheaper and earlier
            # volume names, but still excludes extremely thin or deeply falling
            # names with no relative-volume evidence.
            if (
                5_000_000 <= market_cap <= 50_000_000_000
                and price >= 0.10
                and volume >= 10_000
                and price * volume >= 50_000
                and not (change < -8 and relative_volume < 0.75)
            ):
                early_rank = rank + (2 if relative_volume >= 1.2 else 0)
                early_found[symbol] = max(early_rank, early_found.get(symbol, float("-inf")))

    strict_ranked = sorted(strict_found, key=strict_found.get, reverse=True)
    early_ranked = sorted(
        (symbol for symbol in early_found if symbol not in strict_found),
        key=early_found.get,
        reverse=True,
    )
    strict_slots = min(70, limit)
    selected = strict_ranked[:strict_slots]
    remaining = max(0, limit - len(selected))
    selected.extend(early_ranked[:remaining])
    print(
        "Stock discovery:",
        f"strict={len(strict_found)}",
        f"early={len(early_found)}",
        f"selected={len(selected)}",
    )
    return selected

KRAKEN = "https://api.kraken.com/0/public"
KRAKEN_PAIR_MAP = None
KRAKEN_PAIR_QUOTE_MAP = {}
KRAKEN_TICKER_CACHE = None
KRAKEN_TICKER_CACHE_AT = 0.0
KRAKEN_REQUEST_LOCK = threading.Lock()
KRAKEN_LAST_REQUEST_AT = 0.0
KRAKEN_MIN_REQUEST_INTERVAL_SECONDS = 1.05
# Kraken public Trades/OHLC limits are IP + trading-pair scoped. Only those
# endpoints use independent pair locks; Ticker/AssetPairs stay IP-wide serial.
# A four-request cap prevents large simultaneous REST bursts across pairs.
KRAKEN_PAIR_SCOPED_PACING = os.getenv("JARVIS_KRAKEN_PAIR_SCOPED_PACING", "true").strip().lower() not in ("0", "false", "no")
KRAKEN_PAIR_REQUEST_LOCKS_GUARD = threading.Lock()
KRAKEN_PAIR_REQUEST_LOCKS = {}
KRAKEN_PAIR_LAST_REQUEST_AT = {}
KRAKEN_PAIR_INFLIGHT = threading.BoundedSemaphore(4)
KRAKEN_OHLC_CACHE = HourlyCandleCache(max_entries=300, cache_path=os.getenv("JARVIS_KRAKEN_OHLC_CACHE_PATH", ".cache/kraken_ohlc.json"))


def kraken_get(url: str, **kwargs):
    """Respect Kraken's different pair-scoped vs IP-wide public REST limits.

    Same-pair Trades/OHLC requests remain serialized and paced at >=1.05s
    after completion. Different pairs may be requested concurrently (max 4).
    Disable with JARVIS_KRAKEN_PAIR_SCOPED_PACING=false if needed.
    """
    global KRAKEN_LAST_REQUEST_AT
    endpoint = url.rsplit("/", 1)[-1].split("?", 1)[0]
    params = kwargs.get("params")
    pair = params.get("pair") if isinstance(params, dict) else None
    if KRAKEN_PAIR_SCOPED_PACING and endpoint in ("Trades", "OHLC") and isinstance(pair, str) and pair and "," not in pair:
        pair_key = pair.upper()
        with KRAKEN_PAIR_REQUEST_LOCKS_GUARD:
            pair_lock = KRAKEN_PAIR_REQUEST_LOCKS.setdefault(pair_key, threading.Lock())
        with KRAKEN_PAIR_INFLIGHT:
            with pair_lock:
                last = KRAKEN_PAIR_LAST_REQUEST_AT.get(pair_key, 0.0)
                wait = KRAKEN_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
                    record_scan_metric("kraken_pair_request_pacing", wait, "success")
                try:
                    return HTTP.get(url, **kwargs)
                finally:
                    KRAKEN_PAIR_LAST_REQUEST_AT[pair_key] = time.monotonic()

    # Shared-IP endpoints and malformed/non-pair requests retain the original
    # conservative global pace, including the fail-safe mode above.
    with KRAKEN_REQUEST_LOCK:
        wait = KRAKEN_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - KRAKEN_LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
            record_scan_metric("kraken_request_pacing", wait, "success")
        try:
            return HTTP.get(url, **kwargs)
        finally:
            KRAKEN_LAST_REQUEST_AT = time.monotonic()


def crypto_base(symbol: str) -> str:
    value = str(symbol or "").upper()
    for suffix in ("USDT", "USD", "USDC"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return {"XBT": "BTC", "XDG": "DOGE"}.get(value, value)


def kraken_pairs():
    """Map Jarvis base symbols to Kraken's public spot pair identifiers."""
    global KRAKEN_PAIR_MAP, KRAKEN_PAIR_QUOTE_MAP
    if KRAKEN_PAIR_MAP is not None:
        return KRAKEN_PAIR_MAP
    response = kraken_get(f"{KRAKEN}/AssetPairs", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken AssetPairs: " + "; ".join(payload["error"]))
    pairs = {}
    quote_currencies = {}
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
            quote_currencies[base] = quote
    KRAKEN_PAIR_MAP = {base: value[1] for base, value in pairs.items()}
    KRAKEN_PAIR_QUOTE_MAP = quote_currencies
    return KRAKEN_PAIR_MAP


def discover_crypto_symbols(limit: int = 150) -> list[str]:
    """Discover a broad, multi-tier set of Kraken USD/USDT spot assets.

    Discovery is intentionally permissive: early movers are retained before
    they become top gainers. Liquidity is tiered for ranking, not used as a
    hard $1m gate. Down moves and large advances remain research candidates;
    scan_crypto/CRQS and the existing verified-live BUY gates decide quality.
    """
    excluded_bases = {
        "USDC", "FDUSD", "USDT", "TUSD", "USDP", "DAI", "EUR", "TRY", "BRL",
        "PYUSD", "USDE", "USDS", "UST", "USTC",
    }
    leveraged_suffixes = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
    global KRAKEN_TICKER_CACHE, KRAKEN_TICKER_CACHE_AT
    try:
        pairs = kraken_pairs()
        response = kraken_get(f"{KRAKEN}/Ticker", timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError("Kraken Ticker: " + "; ".join(payload["error"]))
        tickers = payload.get("result") or {}
        KRAKEN_TICKER_CACHE = tickers
        KRAKEN_TICKER_CACHE_AT = time.monotonic()
    except Exception as exc:
        print(f"Kraken crypto discovery unavailable: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return []

    ranked = []
    for base, pair_id in pairs.items():
        if base in excluded_bases or base.endswith(leveraged_suffixes):
            continue
        ticker = tickers.get(pair_id)
        if not ticker:
            continue
        quote_currency = KRAKEN_PAIR_QUOTE_MAP.get(base, "USD")
        price = safe_float((ticker.get("c") or [0])[0])
        volume = safe_float((ticker.get("v") or [0, 0])[1])
        vwap = safe_float((ticker.get("p") or [0, 0])[1])
        quote_volume = volume * vwap
        opening = safe_float(ticker.get("o"))
        change = (price / opening - 1) * 100 if opening > 0 else 0.0
        # Keep a lower-liquidity discovery tier for research, while excluding
        # markets too thin to provide a minimally useful scan reference.
        if price <= 0 or quote_volume < 250_000:
            continue
        # Prefer genuine activity and emerging strength without making a
        # large 24h gain or positive daily return a prerequisite.
        liquidity_points = min(np.log10(max(quote_volume, 1)), 9) * 2
        momentum_points = max(-8.0, min(change, 20.0)) * 0.35
        early_activity_points = 3.0 if quote_volume >= 1_000_000 else 0.0
        rank = liquidity_points + momentum_points + early_activity_points
        ranked.append((rank, quote_volume, base + "USDT"))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [symbol for _, _, symbol in ranked[:max(0, limit)]]
    print(
        "Crypto discovery:",
        f"eligible={len(ranked)}",
        f"selected={len(selected)}",
        "minimum_24h_quote_volume=250000",
        flush=True,
    )
    return selected

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
    except (TypeError, ValueError):        return default

def stooq_daily_history(symbol: str) -> pd.DataFrame:
    """Free backup daily OHLCV history from Stooq; data may be delayed."""
    stooq_symbol = f"{symbol.lower()}.us"
    response = HTTP.get(
        "https://stooq.com/q/d/l/",
        params={"s": stooq_symbol, "i": "d"},
        timeout=20,
    )
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text))
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if frame.empty or not required.issubset(frame.columns):
        raise RuntimeError(f"Stooq returned incomplete daily history for {symbol}")
    frame = frame.rename(columns={name: name.title() for name in required})
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Close", "Volume"]).sort_values("Date").tail(756)
    if len(frame) < 22 or safe_float(frame["Volume"].tail(20).mean()) <= 0:
        raise RuntimeError(f"Stooq returned insufficient usable daily history for {symbol}")
    return frame.reset_index(drop=True)


STOOQ_QUOTE_CACHE = {}
STOOQ_QUOTE_CACHE_LOCK = threading.Lock()
STOOQ_QUOTE_CACHE_TTL_SECONDS = 20
STOOQ_QUOTE_FAILURE_LIMIT = 2
STOOQ_QUOTE_MAX_ATTEMPTS_PER_SCAN = 3
STOOQ_QUOTE_STATE_LOCK = threading.Lock()
STOOQ_QUOTE_STATE = {"attempts": 0, "failures": 0, "circuit_open": False, "last_error": None}


def stooq_latest_quote(symbol: str) -> dict:
    """Fetch an independent Stooq quote with a scan-wide circuit breaker."""
    started = time.monotonic()
    outcome = "failure"
    error_detail = None
    try:
        cache_key = symbol.upper()
        with STOOQ_QUOTE_CACHE_LOCK:
            cached = STOOQ_QUOTE_CACHE.get(cache_key)
            if cached and (time.monotonic() - cached["cached_at"]) <= STOOQ_QUOTE_CACHE_TTL_SECONDS:
                outcome = "success"
                return cached["quote"]

        # Reserve a small number of attempts across all stock workers. This prevents
        # a provider outage from causing one timeout per ticker.
        with STOOQ_QUOTE_STATE_LOCK:
            if STOOQ_QUOTE_STATE["circuit_open"] or STOOQ_QUOTE_STATE["attempts"] >= STOOQ_QUOTE_MAX_ATTEMPTS_PER_SCAN:
                STOOQ_QUOTE_STATE["circuit_open"] = True
                reason = STOOQ_QUOTE_STATE.get("last_error") or "per-scan attempt budget reached"
                record_scan_metric("stooq_quote_validation_skipped", 0, "failure", error=f"circuit_open: {reason}")
                raise RuntimeError("Stooq quote circuit open; independent validation unavailable")
            STOOQ_QUOTE_STATE["attempts"] += 1

        response = HTTP.get(
            "https://stooq.com/q/l/",
            params={"s": f"{symbol.lower()}.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            timeout=(2, 2),
        )
        response.raise_for_status()
        quote = pd.read_csv(StringIO(response.text))
        if quote.empty or not {"Date", "Time", "Close"}.issubset(quote.columns):
            raise RuntimeError("Stooq quote response incomplete")
        row = quote.iloc[0]
        price = safe_float(row.get("Close"))
        if price <= 0:
            raise RuntimeError("Stooq quote has no usable price")
        stamp = pd.Timestamp(f"{row['Date']} {row['Time']}")
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("America/New_York")
        stamp = stamp.tz_convert("UTC")
        age = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds() / 60
        if age < -5 or age > 60:
            raise RuntimeError(f"Stooq quote stale: {age:.0f} minutes")
        result = {"price": price, "as_of": stamp.isoformat(), "age_minutes": round(age, 1)}
        with STOOQ_QUOTE_CACHE_LOCK:
            STOOQ_QUOTE_CACHE[cache_key] = {"quote": result, "cached_at": time.monotonic()}
        outcome = "success"
        return result
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {str(exc)[:180]}"
        with STOOQ_QUOTE_STATE_LOCK:
            STOOQ_QUOTE_STATE["failures"] += 1
            STOOQ_QUOTE_STATE["last_error"] = error_detail
            if STOOQ_QUOTE_STATE["failures"] >= STOOQ_QUOTE_FAILURE_LIMIT or STOOQ_QUOTE_STATE["attempts"] >= STOOQ_QUOTE_MAX_ATTEMPTS_PER_SCAN:
                STOOQ_QUOTE_STATE["circuit_open"] = True
        raise
    finally:
        record_scan_metric("stooq_quote_validation", time.monotonic() - started, outcome, error=error_detail)


ALPACA_QUOTE_CACHE = {}
ALPACA_QUOTE_CACHE_LOCK = threading.Lock()
ALPACA_QUOTE_CACHE_TTL_SECONDS = 10

ALPACA_SNAPSHOT_CACHE = {}
ALPACA_SNAPSHOT_CACHE_LOCK = threading.Lock()

# GTN-A was confirmed by Alpaca to return HTTP 400 even as a singleton
# snapshot request. Keep it in Jarvis discovery/research; skip only this
# provider-specific quote lookup so it cannot poison otherwise-valid batches.
ALPACA_SNAPSHOT_KNOWN_UNSUPPORTED_SYMBOLS = frozenset({"GTN-A"})


def _alpaca_snapshot_request_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    unique = list(dict.fromkeys(str(x).upper() for x in symbols if str(x).strip()))
    skipped = sorted(set(unique) & ALPACA_SNAPSHOT_KNOWN_UNSUPPORTED_SYMBOLS)
    return [symbol for symbol in unique if symbol not in ALPACA_SNAPSHOT_KNOWN_UNSUPPORTED_SYMBOLS], skipped


def prefetch_alpaca_snapshots(symbols: list[str]) -> None:
    """Fetch Alpaca snapshots in batches; isolate invalid symbols instead of losing a whole batch."""
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
    if not api_key or not api_secret or not symbols:
        return
    feed = os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower() or "iex"
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    fetched = {}
    rejected_symbols = []

    def store_snapshot(symbol: str, snapshot: dict) -> None:
        if not isinstance(snapshot, dict):
            return
        trade = snapshot.get("latestTrade") or {}
        trade_price = safe_float(trade.get("p"))
        trade_stamp = pd.Timestamp(trade["t"]) if trade.get("t") else None
        if trade_stamp is not None and pd.notna(trade_stamp):
            if trade_stamp.tzinfo is None:
                trade_stamp = trade_stamp.tz_localize("UTC")
            trade_stamp = trade_stamp.tz_convert("UTC")
            trade_age = (datetime.now(timezone.utc) - trade_stamp.to_pydatetime()).total_seconds() / 60
        else:
            trade_age = None
        quote = snapshot.get("latestQuote") or {}
        bid, ask = safe_float(quote.get("bp")), safe_float(quote.get("ap"))
        quote_stamp = pd.Timestamp(quote["t"]) if quote.get("t") else None
        if quote_stamp is not None and pd.notna(quote_stamp):
            if quote_stamp.tzinfo is None:
                quote_stamp = quote_stamp.tz_localize("UTC")
            quote_stamp = quote_stamp.tz_convert("UTC")
            quote_age = (datetime.now(timezone.utc) - quote_stamp.to_pydatetime()).total_seconds() / 60
        else:
            quote_age = None
        spread = ((ask - bid) / ((ask + bid) / 2) * 100) if bid > 0 and ask >= bid and ask > 0 else None
        daily = snapshot.get("dailyBar") or {}
        daily_price = safe_float(daily.get("c"))
        daily_stamp = pd.Timestamp(daily["t"]) if daily.get("t") else None
        if daily_stamp is not None and pd.notna(daily_stamp):
            if daily_stamp.tzinfo is None:
                daily_stamp = daily_stamp.tz_localize("UTC")
            daily_stamp = daily_stamp.tz_convert("UTC")
        fetched[str(symbol).upper()] = {
            "trade_price": trade_price,
            "trade_as_of": trade_stamp.isoformat() if trade_stamp is not None and pd.notna(trade_stamp) else None,
            "trade_age_minutes": round(trade_age, 2) if trade_age is not None else None,
            "bid": bid if bid > 0 else None,
            "ask": ask if ask > 0 else None,
            "bid_size": safe_float(quote.get("bs")),
            "ask_size": safe_float(quote.get("as")),
            "quote_price": (bid + ask) / 2 if bid > 0 and ask >= bid and ask > 0 else None,
            "quote_as_of": quote_stamp.isoformat() if quote_stamp is not None and pd.notna(quote_stamp) else None,
            "quote_age_minutes": round(quote_age, 2) if quote_age is not None else None,
            "spread_pct": round(spread, 3) if spread is not None else None,
            "daily_price": daily_price,
            "daily_as_of": daily_stamp.isoformat() if daily_stamp is not None and pd.notna(daily_stamp) else None,
            "feed": feed,
        }

    def fetch_batch(batch_symbols: list[str]) -> None:
        if not batch_symbols:
            return
        started = time.monotonic()
        try:
            response = HTTP.get(
                "https://data.alpaca.markets/v2/stocks/snapshots",
                params={"symbols": ",".join(batch_symbols), "feed": feed},
                headers=headers, timeout=(3, 8),
            )
            response.raise_for_status()
            payload = response.json() or {}
            for symbol, snapshot in payload.items():
                store_snapshot(symbol, snapshot)
            record_scan_metric("alpaca_snapshot_batch", time.monotonic() - started)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            # Alpaca rejects an entire snapshots request if one symbol is invalid.
            # Split only HTTP 400 batches to preserve valid symbols without retrying
            # authentication, entitlement, rate-limit, or server failures.
            if status_code == 400 and len(batch_symbols) > 1:
                midpoint = len(batch_symbols) // 2
                record_scan_metric(
                    "alpaca_snapshot_batch_split", time.monotonic() - started,
                    "recovered", error=f"HTTP 400; split batch of {len(batch_symbols)} symbols",
                )
                fetch_batch(batch_symbols[:midpoint])
                fetch_batch(batch_symbols[midpoint:])
                return
            if status_code == 400 and len(batch_symbols) == 1:
                rejected_symbols.append(batch_symbols[0])
            record_scan_metric(
                "alpaca_snapshot_batch", time.monotonic() - started, "failure",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )

    unique_symbols, known_unsupported = _alpaca_snapshot_request_symbols(symbols)
    rejected_symbols.extend(known_unsupported)
    if known_unsupported:
        print(
            "Alpaca snapshot skipped provider-confirmed unsupported symbols: "
            + ",".join(known_unsupported),
            flush=True,
        )
    for offset in range(0, len(unique_symbols), 50):
        fetch_batch(unique_symbols[offset:offset + 50])

    with ALPACA_SNAPSHOT_CACHE_LOCK:
        ALPACA_SNAPSHOT_CACHE.clear()
        ALPACA_SNAPSHOT_CACHE.update(fetched)
    if rejected_symbols:
        print(
            f"Alpaca snapshot rejected unsupported symbols: {','.join(rejected_symbols[:20])}"
            + (f" (+{len(rejected_symbols) - 20} more)" if len(rejected_symbols) > 20 else ""),
            flush=True,
        )
    print(
        f"Alpaca snapshot batch: requested={len(unique_symbols)} returned={len(fetched)} "
        f"rejected={len(rejected_symbols)} feed={feed}",
        flush=True,
    )

def alpaca_snapshot_for(symbol: str) -> Optional[dict]:
    with ALPACA_SNAPSHOT_CACHE_LOCK:
        return ALPACA_SNAPSHOT_CACHE.get(str(symbol).upper())


def alpaca_latest_quote(symbol: str) -> dict:
    """Fetch a fresh independent Alpaca best-bid/ask quote (IEX by default)."""
    started = time.monotonic()
    outcome = "failure"
    error_detail = None
    try:
        api_key = os.getenv("ALPACA_API_KEY", "").strip()
        api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("Alpaca API credentials are not configured")
        cache_key = symbol.upper()
        with ALPACA_QUOTE_CACHE_LOCK:
            cached = ALPACA_QUOTE_CACHE.get(cache_key)
            if cached and time.monotonic() - cached["cached_at"] <= ALPACA_QUOTE_CACHE_TTL_SECONDS:
                outcome = "success"
                return cached["quote"]

        feed = os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower() or "iex"
        if feed not in {"iex", "sip", "delayed_sip", "otc", "boats", "overnight"}:
            raise RuntimeError(f"Unsupported Alpaca stock data feed: {feed}")
        response = HTTP.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/quotes/latest",
            params={"feed": feed},
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=(2, 3),
        )
        response.raise_for_status()
        payload = response.json()
        quote = payload.get("quote") or {}
        bid = safe_float(quote.get("bp"))
        ask = safe_float(quote.get("ap"))
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError("Alpaca quote has no valid two-sided market")
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct > 5.0:
            raise RuntimeError(f"Alpaca quote spread too wide: {spread_pct:.2f}%")
        quote_timestamp = quote.get("t")
        if not quote_timestamp:
            raise RuntimeError("Alpaca quote is missing its exchange timestamp")
        stamp = pd.Timestamp(quote_timestamp)
        if pd.isna(stamp):
            raise RuntimeError("Alpaca quote timestamp is invalid")
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("UTC")
        age = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds() / 60
        if age < -1 or age > 60:
            raise RuntimeError(f"Alpaca quote stale: {age:.1f} minutes")
        result = {
            "price": (bid + ask) / 2,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 3),
            "as_of": stamp.isoformat(),
            "age_minutes": round(age, 2),
            "feed": feed,
        }
        with ALPACA_QUOTE_CACHE_LOCK:
            ALPACA_QUOTE_CACHE[cache_key] = {"quote": result, "cached_at": time.monotonic()}
        outcome = "success"
        return result
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {str(exc)[:180]}"
        raise
    finally:
        record_scan_metric("alpaca_quote_validation", time.monotonic() - started, outcome, error=error_detail)


MASSIVE_REQUEST_LOCK = threading.Lock()
MASSIVE_LAST_REQUEST_AT = 0.0
MASSIVE_REQUESTS_THIS_PROCESS = 0
MASSIVE_MAX_REQUESTS_PER_SCAN = 3
SCAN_METRICS_LOCK = threading.Lock()
SCAN_METRICS = {}
HTTP_STATUS_COUNTS = {}


def reset_scan_metrics() -> None:
    global SCAN_METRICS, HTTP_STATUS_COUNTS, MASSIVE_REQUESTS_THIS_PROCESS, STOOQ_QUOTE_STATE
    with SCAN_METRICS_LOCK:
        SCAN_METRICS = {}
        HTTP_STATUS_COUNTS = {}
    with MASSIVE_REQUEST_LOCK:
        MASSIVE_REQUESTS_THIS_PROCESS = 0
    with STOOQ_QUOTE_STATE_LOCK:
        STOOQ_QUOTE_STATE = {"attempts": 0, "failures": 0, "circuit_open": False, "last_error": None}


def record_scan_metric(name: str, elapsed: float, outcome: str = "success", error: Optional[str] = None) -> None:
    with SCAN_METRICS_LOCK:
        metric = SCAN_METRICS.setdefault(name, {"calls": 0, "failures": 0, "total_seconds": 0.0, "max_seconds": 0.0, "error_counts": {}})
        metric["calls"] += 1
        metric["failures"] += int(outcome != "success")
        metric["total_seconds"] += max(0.0, float(elapsed))
        metric["max_seconds"] = max(metric["max_seconds"], float(elapsed))
        if error:
            label = str(error)[:220]
            metric["error_counts"][label] = metric["error_counts"].get(label, 0) + 1


def record_http_response(response, *args, **kwargs):
    """Capture HTTP status/latency without logging query strings, credentials, or response bodies."""
    try:
        host = (response.url.split("/", 3)[2] if response.url and "://" in response.url else "unknown").lower()
        provider = {
            "api.binance.com": "binance",
            "api.kraken.com": "kraken",
            "api.coinbase.com": "coinbase",
            "api.exchange.coinbase.com": "coinbase_exchange",
            "data.alpaca.markets": "alpaca",
            "api.massive.com": "massive",
            "stooq.com": "stooq",
        }.get(host, host.replace(".", "_")[:48])
        status = int(response.status_code)
        elapsed = response.elapsed.total_seconds() if response.elapsed is not None else 0.0
        retry_after = response.headers.get("Retry-After")
        outcome = "failure" if status >= 400 else "success"
        detail = f"HTTP {status}" + (f"; Retry-After={retry_after}" if retry_after else "")
        with SCAN_METRICS_LOCK:
            key = f"{provider}:{status}"
            HTTP_STATUS_COUNTS[key] = HTTP_STATUS_COUNTS.get(key, 0) + 1
        record_scan_metric(f"http_{provider}_status_{status}", elapsed, outcome, detail if status >= 400 else None)
    except Exception:
        # Diagnostics must never interrupt or alter a market-data request.
        return response
    return response


HTTP.hooks["response"].append(record_http_response)


ENRICHMENT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-enrichment")


def bounded_enrichment_call(callable_, timeout_seconds: float = 8.0):
    """Keep optional news/profile enrichment from holding a stock scan indefinitely."""
    future = ENRICHMENT_EXECUTOR.submit(callable_)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"Optional provider enrichment exceeded {timeout_seconds:.0f}s budget") from exc


def scan_metrics_snapshot() -> dict:
    with SCAN_METRICS_LOCK:
        return {
            "provider_http_status_counts": dict(HTTP_STATUS_COUNTS),
            "provider_http_429_count": sum(count for key, count in HTTP_STATUS_COUNTS.items() if key.endswith(":429")),
            "provider_http_5xx_count": sum(count for key, count in HTTP_STATUS_COUNTS.items() if int(key.rsplit(":", 1)[-1]) >= 500),
            "providers": {
                name: {
                    "calls": values["calls"],
                    "failures": values["failures"],
                    "total_seconds": round(values["total_seconds"], 2),
                    "max_seconds": round(values["max_seconds"], 2),
                    "error_counts": dict(values.get("error_counts", {})),
                }
                for name, values in SCAN_METRICS.items()
            },
        }


MASSIVE_LIVE_QUOTE_ENABLED = os.getenv("MASSIVE_LIVE_QUOTE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def massive_daily_history(symbol: str) -> pd.DataFrame:
    """Free-tier Massive EOD daily bars; rate-limited and only used after Yahoo fails."""
    global MASSIVE_LAST_REQUEST_AT, MASSIVE_REQUESTS_THIS_PROCESS
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is not configured")

    # Basic/free accounts have a low request allowance. Space calls 12 seconds apart
    # and cap this process at 10 fallback requests so an outage cannot stall a scan.
    with MASSIVE_REQUEST_LOCK:
        if MASSIVE_REQUESTS_THIS_PROCESS >= MASSIVE_MAX_REQUESTS_PER_SCAN:
            raise RuntimeError("Massive fallback request budget reached for this scan")
        wait_seconds = max(0.0, 12.0 - (time.monotonic() - MASSIVE_LAST_REQUEST_AT))
        if wait_seconds:
            time.sleep(wait_seconds)
        MASSIVE_LAST_REQUEST_AT = time.monotonic()
        MASSIVE_REQUESTS_THIS_PROCESS += 1

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=120)
    url = (
        f"https://api.massive.com/v2/aggs/ticker/{symbol}/range/1/day/"
        f"{start_date.isoformat()}/{end_date.isoformat()}"
    )
    response = HTTP.get(
        url,
        params={"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": api_key},
        timeout=20,
    )
    if response.status_code != 200:
        # Do not include the request URL: it contains the API key query parameter.
        raise RuntimeError(f"Massive API returned HTTP {response.status_code}")
    payload = response.json()
    results = payload.get("results") or []
    if payload.get("status") not in ("OK", "DELAYED") or not results:
        raise RuntimeError("Massive returned no usable daily aggregates")
    frame = pd.DataFrame(results).rename(
        columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "Timestamp"}
    )
    required = {"Open", "High", "Low", "Close", "Volume", "Timestamp"}
    if not required.issubset(frame.columns):
        raise RuntimeError("Massive returned incomplete OHLCV fields")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Close", "Volume"]).sort_values("Timestamp")
    frame.index = pd.to_datetime(frame.pop("Timestamp"), unit="ms", utc=True)
    if len(frame) < 22 or safe_float(frame["Volume"].tail(20).mean()) <= 0:
        raise RuntimeError("Massive returned insufficient usable daily history")
    return frame.tail(90)


def massive_latest_trade(symbol: str) -> dict:
    """Optional independent Massive last-trade quote; requires an entitled Stocks plan."""
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MASSIVE_API_KEY is not configured")
    response = HTTP.get(
        f"https://api.massive.com/v2/last/trade/{symbol}",
        params={"apiKey": api_key},
        timeout=8,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"Massive last-trade endpoint not entitled (HTTP {response.status_code})")
    response.raise_for_status()
    payload = response.json()
    trade = payload.get("results") or {}
    price = safe_float(trade.get("p"))
    timestamp_ns = safe_float(trade.get("t") or trade.get("y"))
    if price <= 0 or timestamp_ns <= 0:
        raise RuntimeError("Massive last-trade response missing price or timestamp")
    stamp = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds() / 60
    # Allows a 15-minute delayed plan plus normal delivery jitter; older prints are not live validation.
    if age < -5 or age > 45:
        raise RuntimeError(f"Massive last trade stale or future-dated: {age:.0f} minutes")
    return {"price": price, "as_of": stamp.isoformat(), "age_minutes": round(age, 1)}


def stock_daily_history(symbol: str, ticker=None):
    """Yahoo primary, limited Massive EOD backup, then Stooq emergency fallback."""
    ticker = ticker or yf.Ticker(symbol)
    started = time.monotonic()
    try:
        frame = ticker.history(period="2y", interval="1d", auto_adjust=True, timeout=8)
        if not frame.empty and len(frame.dropna(subset=["Close", "Volume"])) >= 22:
            record_scan_metric("yahoo_daily_history", time.monotonic() - started)
            return frame, "Yahoo Finance"
        raise RuntimeError("Yahoo returned insufficient daily history")
    except Exception as primary_error:
        record_scan_metric("yahoo_daily_history", time.monotonic() - started, "failure")
        massive_error = None
        massive_started = time.monotonic()
        try:
            result = massive_daily_history(symbol)
            record_scan_metric("massive_daily_fallback", time.monotonic() - massive_started)
            return result, "Massive daily aggregates (free-tier EOD/delayed; no intraday)"
        except Exception as exc:
            record_scan_metric("massive_daily_fallback", time.monotonic() - massive_started, "failure")
            massive_error = exc
        stooq_started = time.monotonic()
        try:
            result = stooq_daily_history(symbol)
            record_scan_metric("stooq_daily_fallback", time.monotonic() - stooq_started)
            return result, "Stooq public daily data (emergency backup; may be delayed)"
        except Exception as stooq_error:
            record_scan_metric("stooq_daily_fallback", time.monotonic() - stooq_started, "failure")
            raise RuntimeError(
                f"Yahoo failed ({primary_error}); Massive unavailable ({massive_error}); "
                f"Stooq failed ({stooq_error})"
            ) from stooq_error


def market_conditions() -> dict:
    """Broad risk appetite snapshot; missing benchmark data is reported, never guessed."""
    benchmarks = {}
    for symbol in ("SPY", "QQQ", "IWM", "BTC-USD"):
        try:
            try:
                hist = yf.Ticker(symbol).history(period="2mo", interval="1d", auto_adjust=True, timeout=8)
                if hist.empty or len(hist.dropna(subset=["Close"])) < 21:
                    raise RuntimeError("Yahoo returned insufficient benchmark history")
            except Exception:
                if symbol == "BTC-USD":
                    raise
                hist, _ = stock_daily_history(symbol)
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
    item["buy_intelligence"] = build_buy_intelligence(item)
    raw_score = int(item.get("score") or 0)
    legacy_adjusted = max(0, min(100, raw_score + adjustment))
    blend = blend_buy_score(legacy_adjusted, item["buy_intelligence"])
    adjusted = blend["score"]
    item["raw_score"] = raw_score
    item["legacy_score"] = legacy_adjusted
    item["score_model"] = blend["model"]
    item["score_model_note"] = blend["note"]
    item["buy_intelligence"]["affects_production_buy_score"] = blend["applied"]
    item["buy_intelligence"]["can_execute_trades"] = False
    item["buy_intelligence"]["pilot_blend"] = blend
    item["buy_intelligence"]["status"] = (
        "PAPER PILOT — hybrid score active" if blend["applied"]
        else "SHADOW ONLY — legacy score retained"
    )
    item["score"] = adjusted
    item["market_regime"] = regime["regime"]
    item["market_score_adjustment"] = adjustment

    has_confirmation = (
        float(item.get("relative_volume") or 0) >= 1.5
        or float(item.get("intraday_relative_volume") or 0) >= 2
        or bool(item.get("breakout_20d") or item.get("breakout_24h") or item.get("intraday_breakout"))
    )

    if item.get("asset_type") == "stock":
        tech = item.get("technical_analysis") or {}
        rsi = tech.get("rsi_14")
        price = float(item.get("price") or 0)
        ema20 = float(tech.get("ema_20") or 0)
        atr = float(item.get("atr_14_true_range") or 0)
        extension_atr = (price - ema20) / atr if price > 0 and ema20 > 0 and atr > 0 else 0
        # Group correlated price-extension warnings: daily, multi-day and intraday
        # gains are one momentum category, not three independent late-stage signals.
        price_extension = []
        if float(item.get("daily_change_pct") or 0) >= 10: price_extension.append("daily move at least 10%")
        if float(item.get("five_day_change_pct") or 0) >= 25: price_extension.append("five-day move at least 25%")
        if float(item.get("intraday_change_pct") or 0) >= 8: price_extension.append("intraday move at least 8%")
        late_categories = []
        if price_extension: late_categories.append("price extension: " + ", ".join(price_extension))
        if rsi is not None and float(rsi) >= 78: late_categories.append("RSI at least 78")
        if extension_atr >= 2.5: late_categories.append("price at least 2.5 ATR above EMA20")
        late_signals = late_categories

        # One volume category prevents daily and intraday relative volume from
        # satisfying the two-signal early-setup requirement by themselves.
        early_signals = []
        if float(item.get("relative_volume") or 0) >= 1.3 or float(item.get("intraday_relative_volume") or 0) >= 1.5:
            early_signals.append("volume accumulation: daily or intraday relative volume building")
        if tech.get("bollinger_squeeze"): early_signals.append("volatility compression: Bollinger squeeze")
        if tech.get("obv_trend_up_10d"): early_signals.append("accumulation: OBV trend improving")
        if item.get("above_session_vwap") or tech.get("ema_9_above_ema_20"):
            early_signals.append("price positioning: VWAP or short-term EMA trend positive")
        technical_confirmations = [
            bool(tech.get("ema_9_above_ema_20") and item.get("above_20d_average")),            bool(safe_float(tech.get("macd_histogram")) > 0 and tech.get("macd_histogram_rising")),
            bool(rsi is not None and 50 <= float(rsi) <= 72),
            bool(item.get("above_session_vwap") or item.get("vwap_reclaim")),
            bool(tech.get("bollinger_squeeze_expansion")),
            bool(tech.get("obv_trend_up_10d")),
        ]
        buy_volume_confirmed = float(item.get("relative_volume") or 0) >= 1.8 or float(item.get("intraday_relative_volume") or 0) >= 2.0
        buy_breakout_confirmed = bool(item.get("breakout_20d") or item.get("intraday_breakout"))
        buy_technical_count = sum(technical_confirmations)
        buy_conditions = (
            adjusted >= 82 and has_confirmation and buy_volume_confirmed
            and buy_breakout_confirmed and buy_technical_count >= 2
            and regime["regime"] != "RISK-OFF"
            and item.get("intraday_confirmation_available", False)
            and bool(item.get("intraday_trend_up_5bars"))
            and not item.get("risk_flags")
        )
        if len(late_signals) >= 2:
            stage, reasons = "LATE RUNNER — CHASE RISK", late_signals
        elif buy_conditions:
            stage, reasons = "BUY", [
                f"Score {adjusted}/100; independently validated live price.",
                f"Volume and breakout confirmed; {buy_technical_count} technical confirmations.",
                "Intraday trend is positive and market regime is not risk-off.",
            ]
        elif adjusted >= 75 and has_confirmation and regime["regime"] != "RISK-OFF" and item.get("intraday_confirmation_available", True):
            stage, reasons = "ACTIONABLE SETUP", ["High score and confirmation; BUY requirements are not all satisfied."]
        elif adjusted >= 55 and has_confirmation:
            stage, reasons = "DEVELOPING RUNNER", ["Momentum and confirmation are developing without multiple late-stage extension warnings."]
        elif adjusted >= 35 and len(early_signals) >= 2:
            stage, reasons = "EARLY SETUP", early_signals
        elif adjusted >= 35:
            stage, reasons = "EARLY WATCH", ["Some interest is present, but early-stage confirmation is still limited."]
        else:
            stage, reasons = "LOW-CONVICTION WATCH", ["Current evidence is below the early-watch threshold."]
        item["stage_extension_atr"] = round(extension_atr, 2) if extension_atr else None
        item["stage_signals"] = {"early": early_signals, "late": late_signals}
        item["stage_reasons"] = reasons
    else:
        crypto_late = float(item.get("daily_change_pct") or 0) >= 18 or float(item.get("hourly_change_pct") or 0) >= 8
        crypto_buy = (
            adjusted >= 82 and has_confirmation
            and float(item.get("relative_volume") or 0) >= 1.8
            and bool(item.get("breakout_24h")) and bool(item.get("above_24h_average"))
            and float(item.get("hourly_change_pct") or 0) > 0
            and not crypto_late and regime["regime"] != "RISK-OFF"
        )
        if crypto_buy:
            stage, reasons = "BUY", [
                f"Score {adjusted}/100; volume {float(item.get('relative_volume') or 0):.1f}x and 24-hour breakout confirmed.",
                "Positive hourly momentum, price above its 24-hour average, and market regime is not risk-off.",
            ]
        elif adjusted >= 75 and has_confirmation and regime["regime"] != "RISK-OFF":
            stage, reasons = "ACTIONABLE SETUP", ["Strong score and confirmation; full BUY conditions are not all satisfied."]
        elif adjusted >= 55 and has_confirmation:
            stage, reasons = "DEVELOPING SETUP", ["Momentum and confirmation are developing."]
        elif adjusted >= 35:
            stage, reasons = "EARLY WATCH", ["Some interest is present, but confirmation remains limited."]
        else:
            stage, reasons = "LOW-CONVICTION WATCH", ["Current evidence is below the early-watch threshold."]
        item["stage_reasons"] = reasons

    # Data freshness is an entry-confirmation gate, not a discovery filter.
    data_quality = item.get("data_quality") or {}

    # BUY readiness: record every gating condition without relaxing the signal rules.
    if item.get("asset_type") == "stock":
        buy_gate_checks = {
            "score_82": adjusted >= 82,
            "volume_confirmed": buy_volume_confirmed,
            "breakout_confirmed": buy_breakout_confirmed,
            "two_technical_confirmations": buy_technical_count >= 2,
            "market_not_risk_off": regime["regime"] != "RISK-OFF",
            "fresh_intraday_confirmation": bool(item.get("intraday_confirmation_available", False)),
            "five_bar_intraday_trend_up": bool(item.get("intraday_trend_up_5bars")),
            "no_risk_flags": not bool(item.get("risk_flags")),
            "not_chase_risk": len(late_signals) < 2,
            "price_verified": bool(item.get("price_validation_passed", False)),
            "live_data_quality": bool(data_quality.get("buy_eligible", False)),
        }
        buy_gate_reasons = {
            "score_82": f"Score {adjusted}/100; needs 82",
            "volume_confirmed": f"Relative volume {float(item.get('relative_volume') or 0):.2f}x; intraday {float(item.get('intraday_relative_volume') or 0):.2f}x",
            "breakout_confirmed": "20-day or intraday breakout required",
            "two_technical_confirmations": f"{buy_technical_count}/6 technical confirmations",
            "market_not_risk_off": f"Market regime is {regime['regime']}",
            "fresh_intraday_confirmation": "Fresh intraday confirmation unavailable",
            "five_bar_intraday_trend_up": "Five-bar intraday trend is not positive",
            "no_risk_flags": "Risk flags present",
            "not_chase_risk": "Late-runner / chase-risk gate triggered",
            "price_verified": item.get("price_validation_note") or "Price not independently verified",
            "live_data_quality": "Selected price is not confirmed live data for the current session",
        }
    else:
        buy_gate_checks = {
            "score_82": adjusted >= 82,
            "volume_1_8x": float(item.get("relative_volume") or 0) >= 1.8,
            "24h_breakout": bool(item.get("breakout_24h")),
            "above_24h_average": bool(item.get("above_24h_average")),
            "positive_hourly_momentum": float(item.get("hourly_change_pct") or 0) > 0,
            "not_extended": not crypto_late,
            "market_not_risk_off": regime["regime"] != "RISK-OFF",
            "price_verified": bool(item.get("price_validation_passed", False)),
            "live_data_quality": bool(data_quality.get("buy_eligible", False)),
        }
        buy_gate_reasons = {
            "score_82": f"Score {adjusted}/100; needs 82",
            "volume_1_8x": f"Relative volume {float(item.get('relative_volume') or 0):.2f}x; needs 1.8x",
            "24h_breakout": "24-hour breakout not confirmed",
            "above_24h_average": "Price is not above its 24-hour average",
            "positive_hourly_momentum": "Hourly momentum is not positive",
            "not_extended": "Crypto price is extended (18% daily or 8% hourly)",
            "market_not_risk_off": f"Market regime is {regime['regime']}",
            "price_verified": item.get("price_validation_note") or "Price not independently verified",
            "live_data_quality": "Selected price is not confirmed live data for the current session",
        }
    item["buy_readiness"] = {
        "passed": sum(bool(v) for v in buy_gate_checks.values()),
        "total": len(buy_gate_checks),
        "percent": round(100 * sum(bool(v) for v in buy_gate_checks.values()) / len(buy_gate_checks)),
        "all_gates_passed": all(buy_gate_checks.values()),
        "checks": buy_gate_checks,
        "missing_conditions": [buy_gate_reasons[k] for k, v in buy_gate_checks.items() if not v],
    }

    # Preserve the evidence-based discovery stage. Only a BUY candidate is
    # downgraded when strict entry-quality checks are incomplete.
    entry_gate = apply_entry_quality_gate(
        stage, data_quality, bool(item.get("price_validation_passed", False)),
        item.get("buy_readiness") or {},
    )
    stage = entry_gate["stage"]
    item["potential_stage"] = entry_gate["potential_stage"] or item.get("potential_stage")
    item["entry_status"] = entry_gate["entry_status"]
    item["signal_blocked_reason"] = entry_gate["signal_blocked_reason"] or item.get("signal_blocked_reason")
    if entry_gate["signal_blocked_reason"]:
        item["stage_reasons"] = list(item.get("stage_reasons") or []) + [entry_gate["signal_blocked_reason"]]

    item["stage"] = stage
    item["signal"] = stage


def levels(price: float, atr: float, support: float) -> dict:
    # Stops remain anchored below the signal price, but targets and risk
    # percentages are calculated from the conservative upper entry reference.
    risk_unit = max(safe_float(atr), price * 0.02)
    entry_low = price
    entry_high = price + risk_unit * 0.25
    risk_distance = min(1.5 * risk_unit, price * 0.10)
    atr_stop = price - risk_distance
    support_value = safe_float(support)
    support_stop = support_value - 0.15 * risk_unit if 0 < support_value < price else 0.0
    usable_support_stop = support_stop if 0 < support_stop < price and (price - support_stop) <= price * 0.10 else 0.0
    stop_loss = max(atr_stop, usable_support_stop, price * 0.01)
    risk_per_unit = max(0.0, entry_high - stop_loss)
    target_1 = entry_high + 2.0 * risk_per_unit
    target_2 = entry_high + 3.0 * risk_per_unit
    return {
        "watch_entry_low": round(entry_low, 6),
        "watch_entry_high": round(entry_high, 6),
        "invalidation_level": round(stop_loss, 6),
        "stop_loss": round(stop_loss, 6),
        "target_1": round(target_1, 6),
        "target_2": round(target_2, 6),
        "risk_pct": round(risk_per_unit / entry_high * 100, 2) if entry_high > 0 else None,
        "target_1_pct": round((target_1 / entry_high - 1) * 100, 2) if entry_high > 0 else None,
        "target_2_pct": round((target_2 / entry_high - 1) * 100, 2) if entry_high > 0 else None,
        "reward_risk_ratio": 2.0,
        "target_2_reward_risk_ratio": 3.0,
        "calculation": {
            "atr_14": round(safe_float(atr), 6),
            "risk_unit": round(risk_unit, 6),
            "two_percent_price_floor": round(price * 0.02, 6),
            "entry_buffer": round(risk_unit * 0.25, 6),
            "risk_distance": round(risk_distance, 6),
            "atr_stop": round(atr_stop, 6),
            "support_reference": round(support_value, 6) if support_value > 0 else None,
            "support_stop": round(support_stop, 6) if support_stop > 0 else None,
            "stop_source": ("nearby support" if usable_support_stop >= atr_stop and usable_support_stop >= price * 0.01 else ("1% price floor" if price * 0.01 >= atr_stop else "ATR stop")),
            "risk_per_unit": round(risk_per_unit, 6),
        },
        "method": "ATR(14)-based risk unit; stop uses 1.5x risk distance and may tighten to nearby support; targets are 2R and 3R from the upper entry reference.",
    }


SEC_TICKER_MAP = None
SEC_TICKER_MAP_LOCK = threading.Lock()

# SEC's fair-access ceiling is 10 requests/second per user across SEC hosts.
# Keep a conservative global start-to-start interval across all scanner threads
# and both www.sec.gov and data.sec.gov.
SEC_REQUEST_MIN_INTERVAL_SECONDS = 0.16  # 6.25 requests/second maximum
SEC_REQUEST_LOCK = threading.Lock()
SEC_LAST_REQUEST_AT = 0.0


def sec_wait_for_request_slot() -> None:
    """Serialize SEC request starts so concurrent stock workers share one rate limit."""
    global SEC_LAST_REQUEST_AT
    with SEC_REQUEST_LOCK:
        now = time.monotonic()
        wait_seconds = SEC_REQUEST_MIN_INTERVAL_SECONDS - (now - SEC_LAST_REQUEST_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        SEC_LAST_REQUEST_AT = time.monotonic()


def sec_recent_filings(symbol: str, days: int = 14) -> list[dict]:
    """Fetch recent material SEC filing metadata; filing contents remain for user review."""
    global SEC_TICKER_MAP
    contact = os.getenv("SEC_CONTACT_EMAIL", "").strip() or os.getenv("JARVIS_GMAIL_USER", "").strip()
    if not contact:
        return []
    headers = {
        "User-Agent": f"JarvisMarketScanner/2.0 ({contact})",
        "Accept-Encoding": "gzip, deflate",
    }
    started = time.monotonic()
    outcome = "failure"
    try:
        # Single-flight initialization prevents every worker from downloading the
        # same ticker map when a scan starts with an empty process cache.
        with SEC_TICKER_MAP_LOCK:
            if SEC_TICKER_MAP is None:
                sec_wait_for_request_slot()
                mapping_response = HTTP.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=5)
                mapping_response.raise_for_status()
                mapping = mapping_response.json()
                SEC_TICKER_MAP = {
                    str(item.get("ticker", "")).upper(): str(item.get("cik_str", "")).zfill(10)
                    for item in mapping.values()
                    if item.get("ticker") and item.get("cik_str")
                }
        cik = SEC_TICKER_MAP.get(symbol.upper())
        if not cik:
            outcome = "success"
            return []
        sec_wait_for_request_slot()
        response = HTTP.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=headers,
            timeout=5,
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
        outcome = "success"
        return results
    except Exception:
        return []
    finally:
        record_scan_metric("sec_recent_filings", time.monotonic() - started, outcome)

def stock_news(symbol: str) -> list[dict]:
    started = time.monotonic()
    outcome = "failure"
    try:
        items = bounded_enrichment_call(lambda: yf.Ticker(symbol).news, timeout_seconds=8) or []
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
        outcome = "success"
        return (headlines[:3] + sec_recent_filings(symbol))[:6]
    except Exception:
        return sec_recent_filings(symbol)
    finally:
        record_scan_metric("yahoo_stock_news", time.monotonic() - started, outcome)

def stock_fundamental_snapshot(ticker) -> dict:
    """Best-effort public company snapshot; missing fields stay null, never inferred."""
    started = time.monotonic()
    outcome = "failure"
    try:
        info = bounded_enrichment_call(ticker.get_info, timeout_seconds=8) or {}
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
        outcome = "success"
        return {"status": "partial" if available else "unavailable", "fields": available,
                "note": "Yahoo Finance company-profile fields; verify against filings because coverage and freshness vary."}
    except Exception:
        return {"status": "unavailable", "note": "Fundamental provider did not return a usable profile."}
    finally:
        record_scan_metric("yahoo_fundamentals", time.monotonic() - started, outcome)

WEBULL_QUOTE_CACHE = {}
WEBULL_QUOTE_CACHE_LOCK = threading.Lock()
WEBULL_QUOTE_SEMAPHORE = threading.BoundedSemaphore(2)
# Webull OpenAPI token verification was observed remaining PENDING for ~200s.
# Keep this optional provider off by default so it cannot hold up a complete scan.
# Enable only after the Webull app's market-data authorization is confirmed.
WEBULL_QUOTE_ENABLED = os.getenv("WEBULL_QUOTE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# Shadow mode collects read-only Webull quotes but never affects scanner decisions.
WEBULL_SHADOW_MODE = os.getenv("WEBULL_SHADOW_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
WEBULL_SHADOW_MAX_SYMBOLS_PER_SCAN = max(1, min(100, int(os.getenv("WEBULL_SHADOW_MAX_SYMBOLS_PER_SCAN", "30") or 30)))
WEBULL_SHADOW_SYMBOLS = set()


def select_webull_shadow_symbols(symbols: list[str], run_number=None, sample_size=None) -> list[str]:
    """Rotate a bounded Webull sample across the stock universe between scans."""
    unique = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if str(symbol).strip()))
    if not unique:
        return []
    size = max(1, min(len(unique), int(sample_size or WEBULL_SHADOW_MAX_SYMBOLS_PER_SCAN)))
    try:
        sequence = max(1, int(run_number))
    except (TypeError, ValueError):
        sequence = max(1, int(time.time() // 900))
    offset = ((sequence - 1) * size) % len(unique)
    return (unique[offset:] + unique[:offset])[:size]


def compare_webull_yahoo_prices(webull_quote: dict | None, yahoo_price, yahoo_as_of=None, yahoo_age_minutes=None) -> dict:
    """Compare contemporaneous Webull and Yahoo observations without changing scanner price."""
    result = {
        "status": "webull_unavailable" if not webull_quote else "yahoo_unavailable_or_stale",
        "webull_price": safe_float(webull_quote.get("price")) if webull_quote else None,
        "webull_as_of": webull_quote.get("as_of") if webull_quote else None,
        "webull_age_minutes": webull_quote.get("age_minutes") if webull_quote else None,
        "yahoo_price": safe_float(yahoo_price) if yahoo_price is not None else None,
        "yahoo_as_of": yahoo_as_of,
        "yahoo_age_minutes": yahoo_age_minutes,
        "difference_pct": None,
        "timestamp_difference_seconds": None,
    }
    if not webull_quote or result["webull_price"] <= 0:
        return result
    if yahoo_price is None or safe_float(yahoo_price) <= 0 or not yahoo_as_of or yahoo_age_minutes is None or not (-5 <= safe_float(yahoo_age_minutes) <= 45):
        return result
    webull_stamp = pd.Timestamp(result["webull_as_of"])
    yahoo_stamp = pd.Timestamp(yahoo_as_of)
    if webull_stamp.tzinfo is None:
        webull_stamp = webull_stamp.tz_localize("UTC")
    if yahoo_stamp.tzinfo is None:
        yahoo_stamp = yahoo_stamp.tz_localize("UTC")
    midpoint = (result["webull_price"] + result["yahoo_price"]) / 2
    if midpoint <= 0:
        return result
    result["timestamp_difference_seconds"] = round(abs((webull_stamp - yahoo_stamp).total_seconds()), 1)
    result["observed_difference_pct"] = round(abs(result["webull_price"] - result["yahoo_price"]) / midpoint * 100, 4)
    if result["timestamp_difference_seconds"] > 900:
        result["status"] = "timestamps_misaligned"
        return result
    result["status"] = "comparable"
    result["difference_pct"] = result["observed_difference_pct"]
    return result


def persist_webull_yahoo_history(records: list[dict], timestamp: str, run_id=None, path=None) -> dict:
    """Persist a bounded rolling shadow-comparison sample; never feeds trading logic."""
    history_path = Path(path or os.getenv("WEBULL_YAHOO_HISTORY_PATH", "webull_yahoo_comparison.json"))
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history, dict):
            history = {}
    except (OSError, json.JSONDecodeError):
        history = {}
    existing = [item for item in history.get("records", []) if isinstance(item, dict)]
    run_key = str(run_id or timestamp)
    existing = [item for item in existing if str(item.get("run_id") or "") != run_key]
    new_records = []
    for item in records:
        if isinstance(item, dict):
            row = dict(item)
            row["scan_timestamp"] = timestamp
            row["run_id"] = run_key
            new_records.append(row)
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    retained = []
    for item in existing + new_records:
        try:
            observed = datetime.fromisoformat(str(item.get("scan_timestamp")).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if observed >= cutoff:
            retained.append(item)
    retained = retained[-20000:]
    comparable = [safe_float(item.get("difference_pct"), np.nan) for item in retained if item.get("status") == "comparable"]
    comparable = [value for value in comparable if np.isfinite(value)]
    ordered = sorted(comparable)
    def percentile(values, fraction):
        return round(values[min(len(values)-1, int((len(values)-1)*fraction))], 4) if values else None
    summary = {
        "record_count": len(retained), "comparable_count": len(comparable),
        "non_comparable_count": len(retained) - len(comparable),
        "mean_difference_pct": round(float(np.mean(comparable)), 4) if comparable else None,
        "median_difference_pct": percentile(ordered, 0.5), "p90_difference_pct": percentile(ordered, 0.9),
        "within_0_25_pct": sum(value <= 0.25 for value in comparable),
        "within_0_5_pct": sum(value <= 0.5 for value in comparable),
        "within_1_pct": sum(value <= 1.0 for value in comparable),
        "within_2_pct": sum(value <= 2.0 for value in comparable), "retention_days": 14,
    }
    result = {"summary": summary, "recent_records": retained[-100:], "updated_at": timestamp}
    history_path.write_text(json.dumps({"schema_version": 1, **result, "records": retained}, indent=2) + "\n", encoding="utf-8")
    return result


def webull_latest_bar(symbol: str, interval: str = "M1", max_age_minutes: int = 10) -> dict:
    """Optional read-only Webull OpenAPI 1-minute bar for independent quote checking."""
    app_key = os.getenv("WEBULL_APP_KEY", "").strip()
    app_secret = os.getenv("WEBULL_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("Webull OpenAPI credentials are not configured")
    cached = None
    with WEBULL_QUOTE_CACHE_LOCK:
        cached = WEBULL_QUOTE_CACHE.get(symbol)
        if cached and (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() < 30:
            return cached["quote"]

    # SDK handles Webull's request-signature authentication. This client is    # read-only in Jarvis: only the Market Data API is instantiated/called.
    from webull.core.client import ApiClient
    from webull.data.data_client import DataClient
    api_client = ApiClient(app_key, app_secret, "us")
    api_client.add_endpoint("us", "api.webull.com")
    data_client = DataClient(api_client)
    with WEBULL_QUOTE_SEMAPHORE:
        response = data_client.market_data.get_history_bar(symbol, "US_STOCK", interval)
    if getattr(response, "status_code", None) != 200:
        raise RuntimeError("Webull Market Data API did not return HTTP 200")
    payload = response.json()

    candidates = []
    def collect(node):
        if isinstance(node, dict):
            close_value = next((node.get(k) for k in ("close", "c", "last_price", "price") if node.get(k) is not None), None)
            time_value = next((node.get(k) for k in ("time", "timestamp", "bar_time", "trade_time") if node.get(k) is not None), None)
            if close_value is not None and time_value is not None:
                try:
                    try:
                        stamp_value = float(time_value)
                    except (TypeError, ValueError):
                        stamp_value = None
                    if stamp_value is not None and stamp_value > 1e12:
                        stamp = datetime.fromtimestamp(stamp_value / 1000, timezone.utc)
                    elif stamp_value is not None and stamp_value > 1e9:
                        stamp = datetime.fromtimestamp(stamp_value, timezone.utc)
                    else:
                        stamp = pd.Timestamp(str(time_value)).to_pydatetime()
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                        stamp = stamp.astimezone(timezone.utc)
                    close = safe_float(close_value)
                    if close > 0:
                        candidates.append((stamp, close))
                except (TypeError, ValueError, OverflowError):
                    pass
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
    collect(payload)
    if not candidates:
        raise RuntimeError("Webull response contained no parseable OHLCV bars")
    stamp, price = max(candidates, key=lambda item: item[0])
    age_minutes = (datetime.now(timezone.utc) - stamp).total_seconds() / 60
    if age_minutes < -5 or age_minutes > max_age_minutes:
        raise RuntimeError("Webull latest bar is stale or future-dated")
    quote = {"price": price, "as_of": stamp.isoformat(), "age_minutes": round(age_minutes, 1)}
    with WEBULL_QUOTE_CACHE_LOCK:
        WEBULL_QUOTE_CACHE[symbol] = {"quote": quote, "fetched_at": datetime.now(timezone.utc)}
    return quote


def technical_indicators(frame: pd.DataFrame) -> dict:
    """Daily trend, momentum, volatility and volume-accumulation indicators."""
    close = frame["Close"].astype(float).dropna()
    volume = frame.loc[close.index, "Volume"].astype(float).fillna(0)
    if len(close) < 26:
        return {}
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal

    delta = close.diff()
    average_gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    average_loss = -delta.clip(upper=0).rolling(14, min_periods=14).mean()
    rs = average_gain / average_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(average_loss != 0, 100)

    middle = close.rolling(20, min_periods=20).mean()
    deviation = close.rolling(20, min_periods=20).std(ddof=0)
    band_width = ((4 * deviation) / middle.replace(0, np.nan)).dropna()
    current_width = safe_float(band_width.iloc[-1]) if len(band_width) else 0
    width_sample = band_width.tail(60)
    width_percentile = float((width_sample <= current_width).mean()) if len(width_sample) else None
    prior_width = safe_float(band_width.iloc[-2]) if len(band_width) >= 2 else 0
    prior_sample = band_width.tail(61).iloc[:-1]
    prior_squeeze = bool(len(prior_sample) >= 10 and prior_width <= float(prior_sample.quantile(0.25)))
    squeeze_expansion = bool(prior_squeeze and current_width > prior_width * 1.05)

    direction = np.sign(close.diff()).fillna(0)
    obv = (direction * volume).cumsum()
    obv_up = bool(len(obv) >= 11 and obv.iloc[-1] > obv.iloc[-11])

    return {
        "rsi_14": round(safe_float(rsi.iloc[-1]), 2) if pd.notna(rsi.iloc[-1]) else None,
        "macd": round(safe_float(macd.iloc[-1]), 6),
        "macd_signal": round(safe_float(macd_signal.iloc[-1]), 6),
        "macd_histogram": round(safe_float(macd_hist.iloc[-1]), 6),
        "macd_histogram_rising": bool(len(macd_hist) >= 2 and macd_hist.iloc[-1] > macd_hist.iloc[-2]),
        "ema_9": round(safe_float(ema9.iloc[-1]), 6),
        "ema_20": round(safe_float(ema20.iloc[-1]), 6),
        "ema_9_above_ema_20": bool(ema9.iloc[-1] > ema20.iloc[-1]),
        "sma_50": round(safe_float(close.tail(50).mean()), 6) if len(close) >= 50 else None,
        "above_sma_50": bool(close.iloc[-1] > close.tail(50).mean()) if len(close) >= 50 else None,
        "bollinger_band_width_pct": round(current_width * 100, 3) if current_width else None,
        "bollinger_width_percentile_60d": round(width_percentile * 100, 1) if width_percentile is not None else None,
        "bollinger_squeeze": bool(width_percentile is not None and width_percentile <= 0.20),
        "bollinger_squeeze_expansion": squeeze_expansion,
        "obv_trend_up_10d": obv_up,
    }


def intraday_vwap_metrics(frame: pd.DataFrame) -> dict:
    """Calculate session VWAP from available intraday OHLCV bars."""
    if frame is None or frame.empty or not {"High", "Low", "Close", "Volume"}.issubset(frame.columns):
        return {"session_vwap": None, "above_vwap": False, "vwap_reclaim": False}
    session = frame.copy()
    index = session.index
    local_dates = index.tz_convert("America/New_York").date if getattr(index, "tz", None) is not None else index.date
    session = session.loc[local_dates == local_dates[-1]]
    if session.empty:
        return {"session_vwap": None, "above_vwap": False, "vwap_reclaim": False}
    high = session["High"].astype(float)
    low = session["Low"].astype(float)
    close = session["Close"].astype(float)
    volume = session["Volume"].astype(float).fillna(0)
    typical = (high + low + close) / 3
    cumulative_volume = volume.cumsum()
    vwap_series = (typical * volume).cumsum() / cumulative_volume.replace(0, np.nan)
    if vwap_series.dropna().empty:
        return {"session_vwap": None, "above_vwap": False, "vwap_reclaim": False}
    current_vwap = safe_float(vwap_series.iloc[-1])
    above = bool(current_vwap > 0 and close.iloc[-1] > current_vwap)
    reclaim = bool(
        above and len(session) >= 2 and pd.notna(vwap_series.iloc[-2])
        and close.iloc[-2] <= vwap_series.iloc[-2]
    )
    return {
        "session_vwap": round(current_vwap, 6) if current_vwap > 0 else None,
        "above_vwap": above,
        "vwap_reclaim": reclaim,
    }



def adx_dmi_metrics(frame: pd.DataFrame, period: int = 14) -> dict:
    """Wilder-style ADX and directional movement from daily OHLC bars."""
    required = {"High", "Low", "Close"}
    if frame is None or len(frame) < period * 2 + 1 or not required.issubset(frame.columns):
        return {"adx": None, "plus_di": None, "minus_di": None, "adx_dmi_status": "Insufficient daily history"}
    high, low, close = (frame[c].astype(float) for c in ("High", "Low", "Close"))
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    previous_close = close.shift(1)
    tr = pd.concat([(high-low), (high-previous_close).abs(), (low-previous_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    a, p, m = safe_float(adx.iloc[-1], np.nan), safe_float(plus_di.iloc[-1], np.nan), safe_float(minus_di.iloc[-1], np.nan)
    if not all(np.isfinite(x) for x in (a, p, m)):
        return {"adx": None, "plus_di": None, "minus_di": None, "adx_dmi_status": "Insufficient daily history"}
    direction = "bullish" if p > m else "bearish" if m > p else "neutral"
    strength = "strong" if a >= 25 else "developing" if a >= 20 else "weak / choppy"
    return {"adx": round(a, 2), "plus_di": round(p, 2), "minus_di": round(m, 2),
            "adx_dmi_status": f"{strength} trend · {direction} DMI"}


def advanced_research_metrics(frame: pd.DataFrame, intraday: pd.DataFrame, price: float,
                              avg_volume: float, spread_pct: Optional[float] = None) -> dict:
    """Observational research fields. These values never feed the BUY score."""
    result = {}
    dollar_volume = price * avg_volume if price > 0 and avg_volume > 0 else None
    result["dollar_volume"] = round(dollar_volume, 2) if dollar_volume is not None else None
    result["spread_pct"] = round(float(spread_pct), 3) if spread_pct is not None else None
    if dollar_volume is None:
        result["liquidity_status"] = "Dollar volume unavailable"
    elif spread_pct is None:
        result["liquidity_status"] = f"{dollar_volume:,.0f} avg dollar volume · spread unavailable"
    else:
        liquidity = "liquid" if dollar_volume >= 5_000_000 and spread_pct <= 1.0 else "caution" if dollar_volume >= 1_000_000 and spread_pct <= 2.5 else "thin / high execution risk"
        result["liquidity_status"] = f"{liquidity} · {dollar_volume:,.0f} avg dollar volume · {spread_pct:.2f}% spread"
    result.update(adx_dmi_metrics(frame))
    close, high, low = (frame[c].astype(float) for c in ("Close", "High", "Low"))
    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    current_atr = safe_float(atr.iloc[-1]) if len(atr) else 0
    prior_atr = safe_float(atr.iloc[-11:-1].mean()) if len(atr) >= 24 else 0
    atr_contraction = ((prior_atr-current_atr)/prior_atr*100) if prior_atr > 0 else None
    base_high, base_low = safe_float(high.tail(10).max()), safe_float(low.tail(10).min())
    base_tightness = ((base_high-base_low)/price*100) if price > 0 and base_high > 0 else None
    result["atr_contraction_pct"] = round(atr_contraction, 2) if atr_contraction is not None else None
    result["base_tightness_pct"] = round(base_tightness, 2) if base_tightness is not None else None
    if atr_contraction is None or base_tightness is None:
        result["volatility_contraction_status"] = "Insufficient history"
    else:
        contracted = atr_contraction >= 10 and base_tightness <= 20
        result["volatility_contraction_status"] = ("Contraction detected" if contracted else "No qualifying contraction") + f" · ATR contraction {atr_contraction:.1f}% · 10d base {base_tightness:.1f}%"
    result["intraday_rvol_same_time"] = None
    result["same_time_relative_volume"] = None
    result["opening_range_status"] = "Intraday bars unavailable"
    result["anchored_vwap_status"] = "Intraday bars unavailable"
    result["research_sample_high"] = price
    result["research_sample_low"] = price
    if intraday is None or intraday.empty or not {"High", "Low", "Close", "Volume"}.issubset(intraday.columns):
        return result
    bars = intraday.dropna(subset=["Close"]).copy()
    idx = bars.index
    local_idx = idx.tz_convert("America/New_York") if getattr(idx, "tz", None) is not None else idx
    bars["_session_date"] = [x.date() for x in local_idx]
    bars["_tod"] = [x.strftime("%H:%M") for x in local_idx]
    today = max(bars["_session_date"])
    session = bars[bars["_session_date"] == today].copy()
    if session.empty:
        return result
    current = session.iloc[-1]
    result["research_sample_high"] = safe_float(current["High"], price)
    result["research_sample_low"] = safe_float(current["Low"], price)
    prior_same_time = bars[(bars["_session_date"] < today) & (bars["_tod"] == current["_tod"])]["Volume"].astype(float)
    baseline = safe_float(prior_same_time.median()) if len(prior_same_time) >= 2 else 0
    same_time_rvol = safe_float(current["Volume"]) / baseline if baseline > 0 else None
    result["intraday_rvol_same_time"] = round(same_time_rvol, 2) if same_time_rvol is not None else None
    result["same_time_relative_volume"] = result["intraday_rvol_same_time"]
    regular = session[[x.time() >= datetime.strptime("09:30", "%H:%M").time() for x in local_idx[bars.index.get_indexer(session.index)]]]
    opening = regular[[x.time() < datetime.strptime("09:45", "%H:%M").time() for x in local_idx[bars.index.get_indexer(regular.index)]]]
    if not opening.empty:
        or_high, or_low = safe_float(opening["High"].max()), safe_float(opening["Low"].min())
        result["opening_range_high"], result["opening_range_low"] = round(or_high, 6), round(or_low, 6)
        last_price = safe_float(session["Close"].iloc[-1], price)
        after_open = regular[regular.index > opening.index[-1]]
        broke_up, broke_down = last_price > or_high, last_price < or_low
        retested = bool(broke_up and not after_open.empty and (after_open["Low"].astype(float) <= or_high * 1.005).any() and last_price >= or_high)
        state = "15m upside breakout + retest held" if retested else "15m upside breakout" if broke_up else "15m downside break" if broke_down else "inside 15m opening range"
        result["opening_range_status"] = f"{state} · range {or_low:.4f}–{or_high:.4f}"
    else:
        result["opening_range_status"] = "15m opening range not yet complete"
    typical = (session["High"].astype(float)+session["Low"].astype(float)+session["Close"].astype(float))/3
    vol = session["Volume"].astype(float).fillna(0)
    cumulative = vol.cumsum()
    vwap = (typical*vol).cumsum()/cumulative.replace(0, np.nan)
    anchor = safe_float(vwap.iloc[-1]) if len(vwap) else 0
    if anchor > 0:
        result["anchored_vwap"] = round(anchor, 6)
        result["anchored_vwap_status"] = ("Above session-open anchored VWAP" if price >= anchor else "Below session-open anchored VWAP") + f" · {anchor:.4f}"
    return result

def scan_stock(symbol: str) -> Optional[dict]:
    equity_session = market_session_status()
    ticker = yf.Ticker(symbol)
    try:
        frame, price_provider = stock_daily_history(symbol, ticker)
    except Exception:
        raise
    frame = frame.dropna(subset=["Close", "Volume"])
    if len(frame) < 22:
        return None

    close = frame["Close"].astype(float)
    volume = frame["Volume"].astype(float)
    daily_bar_price = safe_float(close.iloc[-1])
    price = daily_bar_price
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
    previous_close = close.shift(1)
    true_range = pd.concat([
        frame["High"].astype(float) - frame["Low"].astype(float),
        (frame["High"].astype(float) - previous_close).abs(),
        (frame["Low"].astype(float) - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = safe_float(true_range.tail(14).mean(), price * 0.03)
    support = safe_float(frame["Low"].tail(10).min(), price * 0.95)
    resistance = safe_float(frame["High"].iloc[-21:-1].max(), price * 1.05)
    technical = technical_indicators(frame)

    # Intraday bars add earlier momentum and pre/post-market context.
    intraday_change = 0.0
    intraday_relative_volume = 0.0
    intraday_breakout = False
    intraday_price = 0.0
    intraday_as_of = None
    intraday_fresh = False
    vwap_metrics = {"session_vwap": None, "above_vwap": False, "vwap_reclaim": False}
    intraday_trend_up = False
    intraday = pd.DataFrame()
    intraday_started = time.monotonic()
    try:
        # Stooq is a daily-only backup; do not pretend it supplies intraday confirmation.
        intraday = ticker.history(period="5d", interval="15m", prepost=True, auto_adjust=True, timeout=8) if price_provider == "Yahoo Finance" else pd.DataFrame()
        if price_provider == "Yahoo Finance":
            record_scan_metric("yahoo_intraday_history", time.monotonic() - intraday_started)
        if not intraday.empty and len(intraday) >= 1:
            intraday = intraday.dropna(subset=["Close"])
            if not intraday.empty:
                intraday_price = safe_float(intraday["Close"].iloc[-1])
                intraday_as_of = pd.Timestamp(intraday.index[-1])
                if intraday_as_of.tzinfo is None:
                    intraday_as_of = intraday_as_of.tz_localize("UTC")
                intraday_as_of = intraday_as_of.tz_convert("UTC")
                quote_age = (datetime.now(timezone.utc) - intraday_as_of.to_pydatetime()).total_seconds() / 60
                intraday_fresh = bool(intraday_price > 0 and -5 <= quote_age <= 45)
            intraday_close = intraday["Close"].astype(float).dropna()
            intraday_volume = intraday["Volume"].astype(float).fillna(0)
            intraday_high = intraday["High"].astype(float).dropna()
            if len(intraday_close) >= 5:
                intraday_trend_up = bool(intraday_close.iloc[-1] > intraday_close.iloc[-5])
            vwap_metrics = intraday_vwap_metrics(intraday)
            if len(intraday_close) >= 5:
                intraday_change = (safe_float(intraday_close.iloc[-1]) / safe_float(intraday_close.iloc[-5]) - 1) * 100
            baseline_volume = safe_float(intraday_volume.iloc[-21:-1].median()) if len(intraday_volume) >= 21 else 0
            current_intraday_volume = safe_float(intraday_volume.iloc[-1])
            intraday_relative_volume = current_intraday_volume / baseline_volume if baseline_volume > 0 else 0
            if len(intraday_high) >= 21:
                intraday_breakout = safe_float(intraday_close.iloc[-1]) > safe_float(intraday_high.iloc[-21:-1].max())
    except Exception:
        if price_provider == "Yahoo Finance":
            record_scan_metric("yahoo_intraday_history", time.monotonic() - intraday_started, "failure")
        # Keep the daily scan usable if intraday data is unavailable.
        pass

    # Collect live quotes independently. Webull must not depend on Yahoo
    # succeeding first, otherwise its fallback path is never actually tested.
    validation_passed = False
    validation_note = "No fresh independently checked intraday quote available"
    secondary_quote = None
    quote_difference_pct = None
    webull_quote = None
    webull_difference_pct = None
    webull_error = None
    webull_status = "not_configured"
    massive_quote = None
    massive_quote_status = "disabled" if not MASSIVE_LIVE_QUOTE_ENABLED else ("not_configured" if not os.getenv("MASSIVE_API_KEY", "").strip() else "not_checked")
    alpaca_quote = None
    alpaca_snapshot = None
    alpaca_quote_status = "not_configured"
    has_alpaca_credentials = bool(
        os.getenv("ALPACA_API_KEY", "").strip()
        and os.getenv("ALPACA_API_SECRET", "").strip()
    )
    if has_alpaca_credentials:
        alpaca_quote_status = "not_checked"
    has_webull_credentials = bool(
        os.getenv("WEBULL_APP_KEY", "").strip()
        and os.getenv("WEBULL_APP_SECRET", "").strip()
    )
    webull_enabled = has_webull_credentials and WEBULL_QUOTE_ENABLED and not WEBULL_SHADOW_MODE
    webull_shadow_requested = bool(WEBULL_SHADOW_MODE and has_webull_credentials and symbol.upper() in WEBULL_SHADOW_SYMBOLS)

    # Stooq's quote endpoint is returning 404s for valid symbols; retain Stooq
    # only for daily-history fallback, not independent live-price validation.

    if has_alpaca_credentials:
        alpaca_snapshot = alpaca_snapshot_for(symbol)
        if alpaca_snapshot:
            trade_age = alpaca_snapshot.get("trade_age_minutes")
            quote_age = alpaca_snapshot.get("quote_age_minutes")
            if alpaca_snapshot.get("trade_price", 0) > 0 and trade_age is not None and -1 <= trade_age <= 15:
                alpaca_quote = {
                    "price": alpaca_snapshot["trade_price"], "as_of": alpaca_snapshot["trade_as_of"],
                    "bid": alpaca_snapshot.get("bid"), "ask": alpaca_snapshot.get("ask"),
                    "age_minutes": trade_age, "spread_pct": alpaca_snapshot.get("spread_pct"),
                    "feed": alpaca_snapshot.get("feed"), "source_type": "latest_trade", "is_live": True,
                }
                alpaca_quote_status = "fresh_trade_received"
            elif (
                alpaca_snapshot.get("quote_price") is not None and quote_age is not None
                and -1 <= quote_age <= 15 and alpaca_snapshot.get("spread_pct") is not None
                and alpaca_snapshot["spread_pct"] <= 5.0
            ):
                alpaca_quote = {
                    "price": alpaca_snapshot["quote_price"], "as_of": alpaca_snapshot["quote_as_of"],
                    "bid": alpaca_snapshot.get("bid"), "ask": alpaca_snapshot.get("ask"),
                    "age_minutes": quote_age, "spread_pct": alpaca_snapshot["spread_pct"],
                    "feed": alpaca_snapshot.get("feed"), "source_type": "latest_quote_midpoint", "is_live": True,
                }
                alpaca_quote_status = "fresh_quote_received"
            else:
                alpaca_quote_status = "no_fresh_trade_or_usable_quote"
        else:
            alpaca_quote_status = "snapshot_unavailable"

    if MASSIVE_LIVE_QUOTE_ENABLED and ((intraday_fresh and intraday_price > 0) or webull_enabled):
        try:
            massive_quote = massive_latest_trade(symbol)
            massive_quote_status = "fresh_quote_received"
        except Exception as exc:            massive_quote_status = "not_entitled" if "not entitled" in str(exc).lower() else "unavailable"

    if webull_enabled or webull_shadow_requested:
        try:
            # Shadow-mode collection is read-only and does not enter the validation path.
            webull_quote = webull_latest_bar(symbol, "M15", 45) if webull_shadow_requested else webull_latest_bar(symbol)
            webull_status = "shadow_quote_received" if webull_shadow_requested else "fresh_quote_received"
        except Exception as exc:
            webull_status = "shadow_quote_unavailable" if webull_shadow_requested else "unavailable_or_not_entitled"
            webull_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            print(f"Webull quote unavailable for {symbol}: {webull_error}", flush=True)
    elif WEBULL_SHADOW_MODE and has_webull_credentials:
        webull_status = "shadow_not_sampled_this_scan"
    elif WEBULL_SHADOW_MODE and not has_webull_credentials:
        webull_status = "shadow_credentials_missing"
    elif has_webull_credentials:
        webull_status = "disabled_pending_authorization"

    reference_quotes = []
    reference_quote_times = {}
    if intraday_fresh and intraday_price > 0:
        reference_quotes.append(("Yahoo", intraday_price))
        reference_quote_times["Yahoo"] = intraday_as_of.isoformat() if intraday_as_of is not None else None
    if secondary_quote and secondary_quote.get("price", 0) > 0:
        reference_quotes.append(("Stooq", secondary_quote["price"]))
        reference_quote_times["Stooq"] = secondary_quote.get("as_of")
    if alpaca_quote and alpaca_quote.get("price", 0) > 0 and alpaca_quote.get("is_live"):
        reference_quotes.append(("Alpaca", alpaca_quote["price"]))
        reference_quote_times["Alpaca"] = alpaca_quote.get("as_of")
    if massive_quote and massive_quote.get("price", 0) > 0:
        reference_quotes.append(("Massive", massive_quote["price"]))
        reference_quote_times["Massive"] = massive_quote.get("as_of")

    reference_spreads = []
    for index, (_, left_price) in enumerate(reference_quotes):
        for _, right_price in reference_quotes[index + 1:]:
            midpoint = (left_price + right_price) / 2
            if midpoint > 0:
                reference_spreads.append(abs(left_price - right_price) / midpoint * 100)
    reference_conflict = any(spread > 3.0 for spread in reference_spreads)

    selected_price_as_of = None
    selected_price_freshness = "daily_bar_unverified"
    selected_data_source = f"{price_provider} daily; live quote unverified"

    if webull_quote and webull_enabled:
        webull_price = webull_quote["price"]
        webull_differences = [
            (name, abs(reference_price - webull_price)
             / ((reference_price + webull_price) / 2) * 100)
            for name, reference_price in reference_quotes
        ]
        if reference_conflict:
            webull_status = "reference_disagreement"
            validation_note = "Independent live quote providers disagree by more than 3%; Webull not accepted"
        elif webull_differences and all(diff <= 3.0 for _, diff in webull_differences):
            validation_passed = True
            webull_difference_pct = max(diff for _, diff in webull_differences)
            webull_status = "confirmed"
            names = "/".join(name for name, _ in webull_differences)
            validation_note = f"Fresh Webull quote cross-checked against {names}; maximum difference {webull_difference_pct:.2f}%"
            price = webull_price
            selected_price_as_of = webull_quote["as_of"]
            selected_price_freshness = "fresh_webull_cross_checked"
            selected_data_source = f"Webull 1m + {names} cross-check"
        elif webull_differences:
            webull_difference_pct = max(diff for _, diff in webull_differences)
            webull_status = "price_mismatch"
            validation_note = f"PRICE DISAGREEMENT: Webull differs from an available reference by {webull_difference_pct:.2f}%"
        else:
            # Fresh Webull-only quotes may be tracked, but are clearly labelled
            # single-source and do not satisfy the separate intraday signal gate.
            validation_passed = True
            webull_status = "fresh_single_source"
            validation_note = "Fresh Webull 1-minute quote accepted; no independent live provider quote was available"
            price = webull_price
            selected_price_as_of = webull_quote["as_of"]
            selected_price_freshness = "fresh_webull_single_source"
            selected_data_source = "Webull 1m quote (single source; no live cross-check)"
    elif len(reference_quotes) >= 2 and not reference_conflict:
        quote_difference_pct = max(reference_spreads) if reference_spreads else 0.0
        selected_reference = next(
            (quote for quote in reference_quotes if quote[0] == "Yahoo"),
            next((quote for quote in reference_quotes if quote[0] == "Massive"), reference_quotes[0]),
        )
        selected_name, selected_price = selected_reference
        validation_passed = True
        validation_note = f"{'/'.join(name for name, _ in reference_quotes)} agree within {quote_difference_pct:.2f}%"
        price = selected_price
        selected_price_as_of = reference_quote_times.get(selected_name)
        selected_price_freshness = "fresh_multi_provider_cross_checked"
        selected_data_source = " + ".join(name for name, _ in reference_quotes) + " independent live/delayed quote cross-check"
    elif reference_conflict:
        validation_note = "Independent live quote providers disagree by more than 3%"
    elif intraday_fresh:
        validation_note = "Fresh Yahoo intraday quote available, but no independent provider quote agreed within 3%"
    elif webull_enabled and not webull_quote:
        validation_note = "Yahoo intraday quote unavailable; Webull quote request also unavailable or not entitled"

    # Whenever the regular session is closed (pre-market, after-hours,
    # overnight, weekends), retain a prior-session close only when Yahoo's
    # completed daily bar independently agrees with Alpaca's daily bar.
    # This is a LAST KNOWN research reference, never a live entry quote:
    # data_quality.buy_eligible remains false and intraday BUY gates stay strict.
    if (
        not validation_passed
        and not equity_session["is_regular_session"]
        and alpaca_snapshot
        and alpaca_snapshot.get("daily_price", 0) > 0
    ):
        alpaca_close = alpaca_snapshot["daily_price"]
        close_diff = abs(daily_bar_price-alpaca_close)/((daily_bar_price+alpaca_close)/2)*100
        if close_diff <= 3.0:
            validation_passed = True
            price = daily_bar_price
            selected_price_as_of = alpaca_snapshot.get("daily_as_of")
            selected_price_freshness = "prior_session_close_cross_checked"
            selected_data_source = "Yahoo + Alpaca prior-session daily close cross-check"
            validation_note = f"Prior-session closes agree within {close_diff:.2f}%; research visibility only while market is closed"

    if validation_passed:
        daily_change = (price / previous - 1) * 100
        five_day_change = (price / safe_float(close.iloc[-6], price) - 1) * 100
    breakout = price > safe_float(close.iloc[-21:-1].max())
    trend_up = price > safe_float(close.tail(20).mean())
    # Five evidence families; correlated measurements compete inside each
    # family rather than stacking. The family caps sum to 100.
    daily_momentum_points = 25 if daily_change >= 2 else 14 if daily_change >= 0.5 else 0
    five_day_momentum_points = 25 if five_day_change >= 5 else 14 if five_day_change >= 2 else 0
    intraday_momentum_points = 25 if intraday_change >= 2 else 12 if intraday_change >= 0.75 else 0
    score_components = {
        "momentum": max(daily_momentum_points, five_day_momentum_points, intraday_momentum_points),
        "volume": max(
            25 if relative_volume >= 2.5 else 18 if relative_volume >= 1.8 else 9 if relative_volume >= 1.3 else 0,
            25 if intraday_relative_volume >= 3 else 16 if intraday_relative_volume >= 2 else 0,
        ),
        "breakout": 20 if (breakout or intraday_breakout) else 0,
        "trend": 15 if trend_up else 0,
    }
    technical_group = (
        (4 if technical.get("ema_9_above_ema_20") and trend_up else 0)
        + (3 if technical.get("macd_histogram", 0) > 0 and technical.get("macd_histogram_rising") else 0)
        + (2 if technical.get("rsi_14") is not None and 50 <= technical["rsi_14"] <= 70 else 0)
        + (2 if vwap_metrics.get("vwap_reclaim") else 0)
        + (3 if technical.get("bollinger_squeeze_expansion") else 0)
        + (1 if technical.get("obv_trend_up_10d") else 0)
    )
    score_components["technical_confirmation"] = min(15, technical_group)
    score = min(100, sum(score_components.values()))

    # Keep valid low-scoring discoveries as watch-only records; strict BUY gates remain unchanged.
    advanced_research = advanced_research_metrics(frame, intraday, price, avg_volume,
        alpaca_quote.get("spread_pct") if alpaca_quote else None)
    news = stock_news(symbol)
    fundamentals = stock_fundamental_snapshot(ticker) if score >= 35 else {"status": "not_fetched_for_low_score"}
    early_runner = compute_early_runner_metrics(frame, intraday, price, fundamentals, news)
    early_warning = compute_early_warning_metrics(frame, intraday, price)
    risk_flags = []
    if not validation_passed:
        risk_flags.append("Price not independently verified; actionable alerts blocked")
    if any(item.get("form") in {"S-1", "S-3", "424B1", "424B3", "424B5"} for item in news):
        risk_flags.append("Potential offering/registration filing detected; review the SEC document")
    received_at = now_iso()
    selected_observed_at = selected_price_as_of or pd.Timestamp(frame.index[-1]).isoformat()
    selected_age_seconds = None
    try:
        selected_age_seconds = (datetime.now(timezone.utc) - pd.Timestamp(selected_observed_at).to_pydatetime().replace(tzinfo=pd.Timestamp(selected_observed_at).tzinfo or timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        selected_age_seconds = None
    data_quality = build_data_quality_record(
        "stock", selected_data_source, selected_price_freshness, selected_observed_at,
        received_at, equity_session, validation_passed,
        alpaca_quote.get("feed") if alpaca_quote else None,
        selected_age_seconds, validation_note
    )
    daily_bar_as_of = pd.Timestamp(frame.index[-1]).isoformat()
    daily_data_quality = build_data_quality_record(
        "stock", price_provider, "daily_bar_unverified", daily_bar_as_of,
        received_at, equity_session, False, None, None,
        "Daily OHLCV history; not an intraday quote"
    )
    intraday_data_quality = build_data_quality_record(
        "stock", "Yahoo Finance intraday bars", "intraday_bar_unclassified",
        intraday_as_of.isoformat() if intraday_as_of is not None else None,
        received_at, equity_session, False, None, None,
        "Yahoo intraday bar delay entitlement is not independently confirmed"
    )
    return {
        "symbol": symbol,
        "asset_type": "stock",
        "price": round(price, 4),
        "price_as_of": selected_price_as_of or pd.Timestamp(frame.index[-1]).isoformat(),
        "price_checked_at": received_at,
        "price_timestamp_type": "daily_session_close" if selected_price_freshness == "prior_session_close_cross_checked" else "exchange_quote",
        "price_session_date": pd.Timestamp(selected_price_as_of).date().isoformat() if selected_price_freshness == "prior_session_close_cross_checked" and selected_price_as_of else None,
        "price_freshness": selected_price_freshness,
        "data_quality": data_quality,
        "daily_data_quality": daily_data_quality,
        "intraday_data_quality": intraday_data_quality,
        "price_age_seconds": selected_age_seconds,
        "price_validation_passed": validation_passed,
        "price_validation_status": (
            "last_known" if selected_price_freshness == "prior_session_close_cross_checked"
            else "verified" if validation_passed else "unverified"
        ),
        "price_validation_note": validation_note,
        "secondary_price": round(secondary_quote["price"], 4) if secondary_quote else None,
        "price_difference_pct": round(quote_difference_pct, 2) if quote_difference_pct is not None else None,
        "webull_price": round(webull_quote["price"], 4) if webull_quote else None,
        "webull_price_as_of": webull_quote["as_of"] if webull_quote else None,
        "webull_price_age_minutes": webull_quote["age_minutes"] if webull_quote else None,
        "webull_price_difference_pct": round(webull_difference_pct, 2) if webull_difference_pct is not None else None,
        "webull_status": webull_status,
        "webull_error": webull_error,
        "webull_yahoo_comparison": compare_webull_yahoo_prices(
            webull_quote, intraday_price if intraday_fresh else None,
            intraday_as_of.isoformat() if intraday_as_of is not None else None,
            (datetime.now(timezone.utc) - intraday_as_of.to_pydatetime()).total_seconds() / 60 if intraday_as_of is not None else None,
        ),
        "massive_price": round(massive_quote["price"], 4) if massive_quote else None,
        "massive_price_as_of": massive_quote["as_of"] if massive_quote else None,
        "massive_price_age_minutes": massive_quote["age_minutes"] if massive_quote else None,
        "massive_quote_status": massive_quote_status,
        "alpaca_price": round(alpaca_quote["price"], 4) if alpaca_quote else None,
        "alpaca_bid": round(alpaca_quote["bid"], 4) if alpaca_quote and alpaca_quote.get("bid") is not None else None,
        "alpaca_ask": round(alpaca_quote["ask"], 4) if alpaca_quote and alpaca_quote.get("ask") is not None else None,
        "alpaca_price_as_of": alpaca_quote["as_of"] if alpaca_quote else None,
        "alpaca_price_age_minutes": alpaca_quote["age_minutes"] if alpaca_quote else None,
        "alpaca_spread_pct": alpaca_quote["spread_pct"] if alpaca_quote else None,
        "alpaca_feed": alpaca_quote["feed"] if alpaca_quote else None,
        "alpaca_quote_status": alpaca_quote_status,
        "stock_execution_quote": ({
            "feed": alpaca_snapshot.get("feed"),
            "as_of": alpaca_snapshot.get("quote_as_of"),
            "bid": alpaca_snapshot.get("bid"),
            "ask": alpaca_snapshot.get("ask"),
            "bid_size": alpaca_snapshot.get("bid_size"),
            "ask_size": alpaca_snapshot.get("ask_size"),
        } if alpaca_snapshot else None),
        "market_session": equity_session,
        "daily_change_pct": round(daily_change, 2),
        "session_change_pct": round(daily_change, 2) if validation_passed else None,
        "five_day_change_pct": round(five_day_change, 2),
        "relative_volume": round(relative_volume, 2),
        "intraday_change_pct": round(intraday_change, 2),
        "intraday_price": round(intraday_price, 4) if intraday_price > 0 else None,
        "intraday_as_of": intraday_as_of.isoformat() if intraday_as_of is not None else None,
        "intraday_relative_volume": round(intraday_relative_volume, 2),
        "intraday_breakout": bool(intraday_breakout),
        "breakout_20d": bool(breakout),
        "above_20d_average": bool(trend_up),
        "technical_analysis": technical,
        **advanced_research,
        "session_vwap": vwap_metrics.get("session_vwap"),
        "above_session_vwap": bool(vwap_metrics.get("above_vwap")),
        "vwap_reclaim": bool(vwap_metrics.get("vwap_reclaim")),
        "intraday_trend_up_5bars": bool(intraday_trend_up),
        "support_10d": round(support, 6),
        "resistance_20d": round(resistance, 6),
        "atr_14_true_range": round(atr, 6),
        "score": score,
        "score_components": score_components,
        "signal": "HIGH INTEREST" if score >= 75 else "WATCH" if score >= 50 else "EARLY WATCH",
        "levels": levels(price, atr, support),
        "news": news,
        "risk_flags": risk_flags,
        "fundamentals": fundamentals,
        **early_runner,
        **early_warning,
        "intraday_confirmation_available": bool(intraday_fresh and equity_session["is_regular_session"]),
        "intraday_quote_available": bool(intraday_fresh),
        "research_notes": [
            "Review latest SEC filings for financing, dilution, debt and material events.",
            "Confirm headline timing and catalyst with a second independent source before acting.",
            "Check float, shares outstanding, cash runway and upcoming earnings; these are not fully verified by this scan.",
        ],
        "data_source": selected_data_source,
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


def coinbase_crypto_market_data(symbol: str, reference_price: Optional[float] = None):
    """Fetch Coinbase USD ticker, 24h stats and hourly candles as a guarded fallback.

    A fresh Coinbase timestamp is mandatory. When a Kraken reference is available,
    Coinbase must also agree within 0.5%. Without an independent Kraken reference,
    data remains watch-only and cannot satisfy the BUY price-verification gate.
    """
    started = time.monotonic()
    outcome = "failure"
    error_detail = None
    try:
        base = crypto_base(symbol)
        product = f"{base}-USD"
        ticker_response = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=(2, 8))
        ticker_response.raise_for_status()
        ticker = ticker_response.json() or {}
        stats_response = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/stats", timeout=(2, 8))
        stats_response.raise_for_status()
        stats = stats_response.json() or {}
        candles_response = HTTP.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 3600},
            timeout=(2, 10),
        )
        candles_response.raise_for_status()
        raw_payload = candles_response.json() or []
        if not isinstance(raw_payload, list):
            raise RuntimeError(f"Coinbase returned invalid hourly candles for {product}")
        # Coinbase returns [time, low, high, open, close, volume], newest first.
        raw = list(reversed(raw_payload))
        bars = [
            [row[0], row[3], row[2], row[1], row[4], row[5]]
            for row in raw
            if isinstance(row, (list, tuple)) and len(row) >= 6
        ]
        if len(bars) < 26:
            raise RuntimeError(f"Coinbase returned insufficient hourly history for {product}: {len(bars)} bars")
        price = safe_float(ticker.get("price"))
        opening = safe_float(stats.get("open"))
        volume = safe_float(stats.get("volume"))
        if price <= 0 or opening <= 0 or volume < 0:
            raise RuntimeError(f"Coinbase returned incomplete market data for {product}")
        stamp = pd.Timestamp(ticker.get("time")) if ticker.get("time") else None
        if stamp is None or pd.isna(stamp):
            raise RuntimeError(f"Coinbase returned no exchange timestamp for {product}")
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("UTC")
        age_seconds = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds()
        fresh = -30 <= age_seconds <= 300
        if not fresh:
            raise RuntimeError(f"Coinbase ticker is stale or future-dated for {product}: {age_seconds:.0f}s")

        reference = safe_float(reference_price)
        difference_pct = None
        agreement = False
        if reference > 0:
            midpoint = (reference + price) / 2
            difference_pct = abs(reference - price) / midpoint * 100 if midpoint > 0 else float("inf")
            agreement = bool(np.isfinite(difference_pct) and difference_pct <= 0.5)
        passed = bool(fresh and agreement)
        if reference > 0 and not agreement:
            note = f"Coinbase fallback rejected: Kraken/Coinbase price difference {difference_pct:.3f}% exceeds 0.5%"
        elif not reference:
            note = "Fresh Coinbase fallback data; no independent Kraken reference, watch-only until cross-validated"
        else:
            note = "Fresh Coinbase USD ticker and hourly candles agree with Kraken within 0.5%"
        normalized = {
            "lastPrice": price,
            "quote_currency": "USD",
            "priceChangePercent": (price / opening - 1) * 100,
            "quoteVolume": volume * price,
            "price_as_of": stamp.isoformat(),
            "price_age_seconds": round(age_seconds, 1),
            "price_validation_passed": passed,
            "price_validation_note": note,
            "price_validation_method": "coinbase_timestamp_kraken_agreement" if passed else "coinbase_fallback_unconfirmed",
            "price_cross_check_difference_pct": round(difference_pct, 4) if difference_pct is not None and np.isfinite(difference_pct) else None,
            "price_validation_reference_source": "Kraken Spot public API" if reference > 0 else None,
            "price_validation_reference_price": reference if reference > 0 else None,
        }
        outcome = "success"
        return normalized, bars, "Coinbase Exchange public API"
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        record_scan_metric("crypto_coinbase_fallback_market_data", time.monotonic() - started, outcome, error=error_detail)




# Free public crypto feeds are collected in shadow mode only. They never enter
# the price-selection, verification, scoring, BUY, or paper-trading paths.
CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN = max(1, min(50, int(os.getenv("CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN", "50") or 50)))
CRYPTO_SHADOW_HISTORY_PATH = os.getenv("CRYPTO_SHADOW_HISTORY_PATH", "crypto_shadow_comparison.json")
# Shadow-only experimental price comparison. The whole Binance.US public
# 24h ticker snapshot can replace dozens of identical per-symbol endpoints.
# Disable via environment variable to restore the original request path.
BINANCE_US_SHADOW_BATCH_ENABLED = os.getenv("JARVIS_BINANCE_US_SHADOW_BATCH_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def fetch_binance_us_shadow_tickers() -> dict:
    """Get one current, complete Binance.US 24h ticker snapshot (shadow only).

    Exchange documentation states the no-symbol form returns every pair.
    Reject incomplete responses and retain the original per-symbol fallback.
    """
    started = time.monotonic()
    outcome = "failure"
    try:
        response = HTTP.get(
            "https://api.binance.us/api/v3/ticker/24hr",
            timeout=(3, 8),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) < 20:
            raise RuntimeError("Binance.US all-market ticker snapshot unavailable or incomplete")
        tickers = {
            str(row.get("symbol") or "").upper(): row
            for row in payload
            if isinstance(row, dict) and row.get("symbol")
        }
        if len(tickers) < 20:
            raise RuntimeError("Binance.US all-market ticker snapshot missing symbols")
        outcome = "success"
        return tickers
    finally:
        record_scan_metric("crypto_shadow_binance_us_bulk_fetch", time.monotonic() - started, outcome)


def select_crypto_shadow_symbols(symbols: list[str], run_number=None, sample_size=None) -> list[str]:
    unique = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if str(symbol).strip()))
    if not unique:
        return []
    size = max(1, min(len(unique), int(sample_size or CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN)))
    try:
        sequence = max(1, int(run_number))
    except (TypeError, ValueError):
        sequence = max(1, int(time.time() // 900))
    offset = ((sequence - 1) * size) % len(unique)
    return (unique[offset:] + unique[:offset])[:size]


def crypto_shadow_quote(provider: str, symbol: str, quote_currency: str = "USD", binance_us_snapshot=None) -> dict:
    """Fetch one free public spot ticker in the same quote currency as Jarvis.\n\n    This observation is diagnostic only and is never used to select a price.\n    """
    base = crypto_base(symbol)
    started = time.monotonic()
    outcome = "failure"
    error_detail = None
    try:
        if provider == "coinbase":
            response = HTTP.get(
                f"https://api.exchange.coinbase.com/products/{base}-{quote_currency}/ticker",
                timeout=(2, 5),
            )
            response.raise_for_status()
            payload = response.json() or {}
            price = safe_float(payload.get("price"))
            stamp = pd.Timestamp(payload.get("time")) if payload.get("time") else None
            bid, ask = safe_float(payload.get("bid")), safe_float(payload.get("ask"))
        elif provider == "binance_us":
            if binance_us_snapshot is not None:
                # The same response schema as the individual 24h ticker path.
                # Missing pairs are explicitly unavailable; never invent prices.
                payload = binance_us_snapshot.get(base + quote_currency)
                if payload is None:
                    raise RuntimeError(f"Binance.US pair is absent from current ticker snapshot: {base + quote_currency}")
            else:
                response = HTTP.get(
                    "https://api.binance.us/api/v3/ticker/24hr",
                    params={"symbol": base + quote_currency},
                    timeout=(2, 5),
                )
                response.raise_for_status()
                payload = response.json() or {}
            price = safe_float(payload.get("lastPrice"))
            stamp = pd.to_datetime(payload.get("closeTime"), unit="ms", utc=True) if payload.get("closeTime") is not None else None
            # Binance 24h ticker has no bid/ask fields; use numeric sentinels so spread logic remains safe.
            bid = ask = 0.0
        else:
            raise ValueError(f"Unsupported crypto shadow provider: {provider}")
        if price <= 0 or stamp is None or pd.isna(stamp):
            raise RuntimeError(f"{provider} returned no usable price or exchange timestamp")
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("UTC")
        age = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds()
        if age < -30 or age > 300:
            raise RuntimeError(f"{provider} quote stale or future-dated: {age:.0f}s")
        spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100) if bid > 0 and ask >= bid and ask > 0 else None
        outcome = "success"
        return {
            "provider": provider, "symbol": symbol, "base": base, "quote_currency": quote_currency,
            "price": price, "as_of": stamp.isoformat(), "age_seconds": round(age, 1),
            "bid": bid if bid > 0 else None, "ask": ask if ask > 0 else None,
            "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "status": "fresh",
        }
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        record_scan_metric(f"crypto_shadow_{provider}", time.monotonic() - started, outcome, error=error_detail)


def compare_crypto_shadow_quote(provider: str, symbol: str, primary_price, primary_as_of, quote=None, error=None, primary_quote_currency="USD", primary_source=None) -> dict:
    primary = safe_float(primary_price)
    result = {
        "provider": provider, "symbol": symbol,
        "primary_price": primary if primary > 0 else None,
        "primary_source": primary_source,
        "primary_as_of": primary_as_of,
        "shadow_price": quote.get("price") if quote else None,
        "shadow_as_of": quote.get("as_of") if quote else None,
        "shadow_age_seconds": quote.get("age_seconds") if quote else None,
        "quote_currency": primary_quote_currency,
        "spread_pct": quote.get("spread_pct") if quote else None,
        "difference_pct": None,
        "status": "unavailable" if error or not quote else "primary_unavailable",
        "error": str(error)[:180] if error else None,
    }
    if quote and provider == "coinbase" and str(primary_source or "").lower().startswith("coinbase"):
        result["status"] = "same_as_primary_source"
    elif quote and quote.get("quote_currency") != primary_quote_currency:
        result["status"] = "quote_currency_mismatch"
    elif quote and primary > 0:
        midpoint = (primary + quote["price"]) / 2
        if midpoint > 0:
            result["difference_pct"] = round(abs(primary - quote["price"]) / midpoint * 100, 4)
            result["status"] = "comparable"
    return result


def persist_crypto_shadow_history(records: list[dict], timestamp: str, run_id=None, path=None) -> dict:
    history_path = Path(path or CRYPTO_SHADOW_HISTORY_PATH)
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history, dict):
            history = {}
    except (OSError, json.JSONDecodeError):
        history = {}
    run_key = str(run_id or timestamp)
    existing = [x for x in history.get("records", []) if isinstance(x, dict) and str(x.get("run_id") or "") != run_key]
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    retained = []
    for item in existing + [{**x, "scan_timestamp": timestamp, "run_id": run_key} for x in records if isinstance(x, dict)]:
        try:
            observed = datetime.fromisoformat(str(item.get("scan_timestamp")).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if observed >= cutoff:
            retained.append(item)
    retained = retained[-20000:]
    providers = {}
    for name in ("coinbase", "binance_us"):
        rows = [x for x in retained if x.get("provider") == name]
        comparable = [safe_float(x.get("difference_pct"), np.nan) for x in rows if x.get("status") == "comparable"]
        comparable = [x for x in comparable if np.isfinite(x)]
        providers[name] = {
            "samples": len(rows), "comparable": len(comparable),
            "unavailable": sum(x.get("status") == "unavailable" for x in rows),
            "mean_difference_pct": round(float(np.mean(comparable)), 4) if comparable else None,
            "median_difference_pct": round(float(np.median(comparable)), 4) if comparable else None,
            "within_0_5_pct": sum(x <= 0.5 for x in comparable),
        }
    result = {"summary": {"record_count": len(retained), "providers": providers, "retention_days": 14},
              "recent_records": retained[-100:], "updated_at": timestamp}
    history_path.write_text(json.dumps({"schema_version": 1, **result, "records": retained}, indent=2) + "\n", encoding="utf-8")
    return result


def collect_crypto_shadow_comparisons(crypto_results: list[dict], run_number=None, run_id=None) -> dict:
    selected = select_crypto_shadow_symbols([x.get("symbol") for x in crypto_results], run_number)
    by_symbol = {str(x.get("symbol", "")).upper(): x for x in crypto_results}
    records = []
    jobs = {}
    binance_us_snapshot = None
    if selected and BINANCE_US_SHADOW_BATCH_ENABLED:
        try:
            binance_us_snapshot = fetch_binance_us_shadow_tickers()
        except Exception as exc:
            # Do not drop shadow observations when the bulk endpoint is down.
            # The individual original ticker requests below remain the fallback.
            print(f"Binance.US shadow batch unavailable; using individual tickers: {type(exc).__name__}", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for symbol in selected:
            item = by_symbol.get(symbol, {})
            quote_currency = str(item.get("price_quote_currency") or "USD").upper()
            for provider in ("coinbase", "binance_us"):
                if provider == "binance_us" and binance_us_snapshot is not None:
                    future = pool.submit(crypto_shadow_quote, provider, symbol, quote_currency, binance_us_snapshot)
                else:
                    future = pool.submit(crypto_shadow_quote, provider, symbol, quote_currency)
                jobs[future] = (provider, symbol, item, quote_currency)
        for future in as_completed(jobs):
            provider, symbol, item, quote_currency = jobs[future]
            quote, error = None, None
            try:
                quote = future.result()
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
            records.append(compare_crypto_shadow_quote(
                provider, symbol, item.get("price"), item.get("price_as_of"), quote, error, quote_currency, item.get("price_source")
            ))
    history = persist_crypto_shadow_history(records, now_iso(), run_id)
    return {"mode": "shadow_only", "sample_limit": CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN,
            "symbols_sampled": len(selected), "records_this_scan": len(records),
            "summary": history["summary"], "recent_records": history["recent_records"]}



def kraken_crypto_market_data(symbol: str):
    """Primary public market data from Kraken Spot REST API."""
    base = crypto_base(symbol)
    pair_id = kraken_pairs().get(base)
    if not pair_id:
        raise RuntimeError(f"Kraken has no supported USD/USDT spot pair for {base}")
    cached_tickers = KRAKEN_TICKER_CACHE
    cache_age = time.monotonic() - KRAKEN_TICKER_CACHE_AT if cached_tickers is not None else float("inf")
    if cached_tickers is not None and cache_age <= 300 and pair_id in cached_tickers:
        ticker = cached_tickers[pair_id]
        record_scan_metric("kraken_ticker_cache_hit", 0.0, "success")
    else:
        ticker_response = kraken_get(f"{KRAKEN}/Ticker", params={"pair": pair_id}, timeout=12)
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
    # Kraken hourly candles only change when a new UTC hour opens. Reuse the
    # same completed-hour context between scans, but refresh at each UTC hour
    # boundary. The independent Trades request below remains fresh on every scan.
    cache_key = (pair_id, 60)
    raw = KRAKEN_OHLC_CACHE.get(cache_key)
    if raw is not None:
        record_scan_metric("kraken_ohlc_cache_hit", 0.0, "success")
    else:
        bars_response = kraken_get(
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
        KRAKEN_OHLC_CACHE.put(cache_key, raw)
        record_scan_metric("kraken_ohlc_cache_miss", 0.0, "success")
    # Kraken returns [time, open, high, low, close, vwap, volume, count].
    bars = [[row[0], row[1], row[2], row[3], row[4], row[6]] for row in raw if len(row) >= 8]
    # Kraken's ticker payload has no quote timestamp. Cross-check its last
    # price against the newest public trade and use that trade's exchange time.
    trades_response = kraken_get(f"{KRAKEN}/Trades", params={"pair": pair_id}, timeout=12)
    trades_response.raise_for_status()
    trades_payload = trades_response.json()
    if trades_payload.get("error"):
        raise RuntimeError("Kraken Trades: " + "; ".join(trades_payload["error"]))
    trade_rows = [value for key, value in (trades_payload.get("result") or {}).items() if key != "last"]
    latest_trade = trade_rows[0][-1] if trade_rows and trade_rows[0] else None
    trade_price = safe_float(latest_trade[0]) if latest_trade else 0.0
    trade_epoch = safe_float(latest_trade[2]) if latest_trade and len(latest_trade) > 2 else 0.0
    age_seconds = datetime.now(timezone.utc).timestamp() - trade_epoch if trade_epoch > 0 else None
    difference_pct = abs(price / trade_price - 1) * 100 if trade_price > 0 else None
    passed = bool(age_seconds is not None and -30 <= age_seconds <= 300 and difference_pct is not None and difference_pct <= 0.5)
    normalized = {
        "lastPrice": price,
        "priceChangePercent": (price / opening - 1) * 100,
        "quoteVolume": volume * vwap,
        "price_as_of": datetime.fromtimestamp(trade_epoch, timezone.utc).isoformat() if trade_epoch > 0 else None,
        "price_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "price_validation_passed": passed,
        "price_validation_note": (
            "Fresh Kraken last trade cross-check passed"
            if passed else
            f"Kraken ticker not validated: trade age={round(age_seconds, 1) if age_seconds is not None else 'unknown'}s; ticker/trade difference={round(difference_pct, 3) if difference_pct is not None else 'unknown'}%"
        ),
        "price_validation_method": "kraken_latest_trade_cross_check",
        "price_cross_check_difference_pct": round(difference_pct, 4) if difference_pct is not None else None,
    }
    return normalized, bars, "Kraken Spot public API"



def coinbase_crypto_price_validation(symbol: str, primary_price: float) -> dict:
    """Independently validate a Kraken-selected price with a fresh Coinbase ticker."""
    started = time.monotonic()
    outcome = "failure"
    error_detail = None
    try:
        base = crypto_base(symbol)
        response = HTTP.get(
            f"https://api.exchange.coinbase.com/products/{base}-USD/ticker",
            timeout=(2, 5),
        )
        response.raise_for_status()
        payload = response.json() or {}
        reference_price = safe_float(payload.get("price"))
        stamp = pd.Timestamp(payload.get("time")) if payload.get("time") else None
        if reference_price <= 0 or stamp is None or pd.isna(stamp):
            raise RuntimeError("Coinbase returned no usable price or exchange timestamp")
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("UTC")
        age_seconds = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds()
        primary = safe_float(primary_price)
        midpoint = (primary + reference_price) / 2
        difference_pct = abs(primary - reference_price) / midpoint * 100 if midpoint > 0 else float("inf")
        fresh = -30 <= age_seconds <= 300
        passed = bool(fresh and difference_pct <= 0.5)
        outcome = "success" if fresh else "failure"
        return {
            "passed": passed,
            "price": reference_price,
            "as_of": stamp.isoformat(),
            "age_seconds": round(age_seconds, 1),
            "difference_pct": round(difference_pct, 4) if np.isfinite(difference_pct) else None,
            "note": (
                "Fresh Coinbase Exchange ticker independently confirms the Kraken-selected price"
                if passed else
                f"Coinbase secondary check did not confirm Kraken price: fresh={fresh}; difference={difference_pct:.3f}%"
            ),
        }
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        record_scan_metric("crypto_coinbase_secondary_validation", time.monotonic() - started, outcome, error=error_detail)

def scan_crypto(symbol: str) -> Optional[dict]:
    try:
        ticker, bars, provider = kraken_crypto_market_data(symbol)
    except Exception as primary_error:
        try:
            # If Kraken is unavailable there is no independent reference price.
            # Coinbase may still provide a watch-only candidate, never a BUY-eligible quote.
            ticker, bars, provider = coinbase_crypto_market_data(symbol)
            ticker["price_validation_note"] = (
                str(ticker.get("price_validation_note") or "")
                + "; Kraken unavailable, so independent price agreement could not be established"
            ).strip("; ")
        except Exception as backup_error:
            raise RuntimeError(
                f"Kraken primary failed ({primary_error}); Coinbase backup failed ({backup_error})"
            ) from backup_error

    if provider == "Kraken Spot public API" and not ticker.get("price_validation_passed"):
        try:
            # Use Coinbase's complete USD market-data set, not just its ticker,
            # when Kraken's own trade/ticker freshness or agreement check fails.
            kraken_age = safe_float(ticker.get("price_age_seconds"), -1)
            kraken_reference = (
                ticker.get("lastPrice")
                if -30 <= kraken_age <= 300 else None
            )
            fallback, fallback_bars, fallback_provider = coinbase_crypto_market_data(
                symbol, reference_price=kraken_reference
            )
            if fallback.get("price_validation_passed") or not kraken_reference:
                # A fresh Coinbase-only fallback may replace a stale/unavailable
                # Kraken quote for research, but remains unverified and watch-only.
                ticker, bars, provider = fallback, fallback_bars, fallback_provider
            else:
                ticker["price_validation_reference_source"] = "Coinbase Exchange public API"
                ticker["price_validation_reference_price"] = fallback.get("lastPrice")
                ticker["price_cross_check_difference_pct"] = fallback.get("price_cross_check_difference_pct")
                ticker["price_validation_note"] = (
                    str(ticker.get("price_validation_note") or "Kraken validation failed")
                    + "; " + str(fallback.get("price_validation_note") or "Coinbase fallback did not validate")
                )
        except Exception as fallback_error:
            ticker["price_validation_note"] = (
                str(ticker.get("price_validation_note") or "Kraken validation failed")
                + f"; Coinbase full fallback unavailable: {type(fallback_error).__name__}: {str(fallback_error)[:120]}"
            )

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

    # Keep the legacy score during a parallel CRQS validation period.
    momentum_points = max(
        30 if change_1h >= 1.5 else 16 if change_1h >= 0.5 else 0,
        30 if change_24h >= 5 else 16 if change_24h >= 2 else 0,
    )
    volume_points = 25 if relative_volume >= 2.5 else 18 if relative_volume >= 1.8 else 9 if relative_volume >= 1.3 else 0
    breakout_points = 25 if breakout else 0
    trend_points = 20 if trend_up else 0
    score_components = {"momentum": momentum_points, "volume": volume_points, "breakout": breakout_points, "trend": trend_points}
    score = min(100, sum(score_components.values()))

    # Crypto Runner Quality Score (CRQS), independent of legacy score and BUY logic.
    four_hour_change = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 and closes[-5] else 0.0
    positive_volume_bars = sum(1 for v in volumes[-6:] if v > 0)
    volume_consistency = positive_volume_bars / 6.0
    quote_volume = safe_float(ticker.get("quoteVolume"))
    momentum_crqs = (8 if change_1h >= 1.0 else 4 if change_1h > 0 else 0) + (7 if four_hour_change >= 2.0 else 3 if four_hour_change > 0 else 0) + (5 if change_24h >= 3 else 2 if change_24h > 0 else 0) + (5 if breakout else 0)
    volume_crqs = (8 if relative_volume >= 2.5 else 5 if relative_volume >= 1.5 else 0) + (4 if volume_consistency >= 0.8 else 2 if volume_consistency >= 0.5 else 0) + (5 if quote_volume >= 5_000_000 else 3 if quote_volume >= 1_000_000 else 0) + (3 if relative_volume >= 1.5 and abs(change_1h) <= 5 else 0)
    higher_lows = len(lows) >= 6 and lows[-1] > lows[-4] and lows[-4] > lows[-6]
    structure_crqs = (5 if higher_lows else 0) + (5 if breakout else 0) + (5 if trend_up else 0) + (5 if abs(change_1h) <= 3 and change_24h > 0 else 0)
    # Relative strength vs BTC/market and sector/adoption metrics are unavailable in this scan and receive zero.
    relative_strength_crqs = 0
    news = crypto_news(symbol)
    catalyst_crqs = 4 if news else 0
    # Missing spread/depth/token-unlock data receives no credit. Timestamp validation earns integrity credit.
    risk_crqs = (3 if ticker.get("price_validation_passed") else 0) + (2 if len(highs) >= 5 and (max(highs[-5:])-min(lows[-5:]))/max(price, 1e-12) < 0.12 else 0)
    crqs_components = {"momentum_ignition": momentum_crqs, "volume_liquidity": volume_crqs, "market_structure": structure_crqs, "relative_strength": relative_strength_crqs, "catalyst_narrative": catalyst_crqs, "risk_integrity": risk_crqs}
    crqs = min(74, sum(crqs_components.values()))
    entry_penalty = 20 if change_24h >= 25 else 12 if change_24h >= 15 else 0
    entry_score = max(0, min(74, crqs - entry_penalty))
    crqs_tier = "Elite Runner" if crqs >= 65 else "High Conviction" if crqs >= 58 else "Developing Runner" if crqs >= 50 else "Early Discovery" if crqs >= 42 else "Watch" if crqs >= 32 else "Low Quality"
    crqs_data_coverage = {"relative_strength_vs_btc": False, "market_relative_strength": False, "sector_activity": False, "spread_and_depth": False, "token_unlock_supply_risk": False, "news_catalyst": True, "hourly_ohlcv": True, "exchange_price_validation": bool(ticker.get("price_validation_passed"))}
    # Keep valid low-scoring discoveries as watch-only records; strict BUY gates remain unchanged.

    received_at = now_iso()
    data_quality = build_data_quality_record(
        "crypto", provider, ticker.get("price_freshness"),
        ticker.get("price_as_of"), received_at, {"phase": "24/7 CRYPTO"},
        bool(ticker.get("price_validation_passed")), None,
        ticker.get("price_age_seconds"), ticker.get("price_validation_note")
    )
    latest_closed_bar_at = None
    try:
        latest_closed_bar_at = datetime.fromtimestamp(float(bars[-2][0]), timezone.utc).isoformat()
    except (IndexError, TypeError, ValueError, OverflowError):
        pass

    # Apply the same early-warning checks to crypto using completed hourly bars.
    # Aggregate only completed UTC days so a partial daily candle cannot create
    # false multi-timeframe confirmation.
    early_warning_bars = pd.DataFrame(
        closed, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"]
    )
    if not early_warning_bars.empty:
        early_warning_bars.index = pd.to_datetime(
            early_warning_bars.pop("Timestamp"), unit="s", utc=True
        )
        daily_warning_bars = early_warning_bars.resample("1D").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna(subset=["Open", "High", "Low", "Close"])
        current_utc_day = pd.Timestamp.now(tz="UTC").normalize()
        daily_warning_bars = daily_warning_bars[daily_warning_bars.index < current_utc_day]
    else:
        daily_warning_bars = pd.DataFrame()
    early_warning = compute_early_warning_metrics(
        daily_warning_bars, early_warning_bars, price
    )

    hourly_data_quality = {
        "data_source": provider + " hourly OHLCV",
        "data_type": "historical" if latest_closed_bar_at else "unknown",
        "observed_at": latest_closed_bar_at,
        "received_at": received_at,
        "age_seconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(latest_closed_bar_at)).total_seconds(), 1) if latest_closed_bar_at else None,
        "session": "24/7 CRYPTO",
        "verification_status": "verified" if latest_closed_bar_at else "unknown",
        "buy_eligible": False,
        "note": "Closed hourly candles are historical indicator inputs, not a live entry quote."
    }
    return {
        "symbol": symbol,
        "asset_type": "crypto",
        "price": price,
        "price_as_of": ticker.get("price_as_of"),
        "price_checked_at": received_at,
        "price_timestamp_type": "exchange_trade",
        "price_session_date": None,
        "price_freshness": "exchange_timestamped_ticker" if ticker.get("price_validation_passed") else "timestamp_missing_or_stale",
        "data_quality": data_quality,
        "hourly_data_quality": hourly_data_quality,
        "price_validation_passed": bool(ticker.get("price_validation_passed")),
        "price_validation_note": ticker.get("price_validation_note") or "No exchange timestamp validation available",
        "price_validation_method": ticker.get("price_validation_method"),
        "price_age_seconds": ticker.get("price_age_seconds"),
        "price_cross_check_difference_pct": ticker.get("price_cross_check_difference_pct"),
        "price_validation_reference_source": ticker.get("price_validation_reference_source"),
        "price_validation_reference_price": ticker.get("price_validation_reference_price"),
        "price_source": provider,
        "price_quote_currency": ticker.get("quote_currency") or "USD",
        "daily_change_pct": round(change_24h, 2),
        "hourly_change_pct": round(change_1h, 2),
        "relative_volume": round(relative_volume, 2),
        "breakout_24h": bool(breakout),
        "above_24h_average": bool(trend_up),
        "quote_volume_24h": round(safe_float(ticker.get("quoteVolume")), 2),
        "crqs": crqs,
        "crqs_tier": crqs_tier,
        "crqs_components": crqs_components,
        "crqs_data_coverage": crqs_data_coverage,
        "crqs_entry_score": entry_score,
        "crqs_entry_penalty": entry_penalty,
        "crqs_features": {"four_hour_change_pct": round(four_hour_change, 2), "higher_lows": bool(higher_lows), "volume_consistency_6h": round(volume_consistency, 3), "news_count": len(news)},
        **early_warning,
        "score": score,
        "signal": "HIGH INTEREST" if score >= 75 else "WATCH" if score >= 50 else "EARLY WATCH",
        "levels": levels(price, atr, support),
        "news": news,
        "data_source": f"{provider} + Yahoo Finance headlines",
    }


def kraken_crypto_cross_section(crypto_sectors: dict) -> dict:
    """Fetch broad Kraken 24h tickers, then summarize independently of candidate selection."""
    pair_map = kraken_pairs()
    response = kraken_get(f"{KRAKEN}/Ticker", timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Kraken breadth ticker: " + "; ".join(payload["error"]))
    return summarize_crypto_cross_section(crypto_sectors, pair_map, payload.get("result") or {})


def run_parallel(symbols: list[str], scanner, workers: int = 6) -> tuple[list, list]:
    results, errors = [], []
    parallel_started = time.monotonic()
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
    record_scan_metric(f"{scanner.__name__}_parallel_wall", time.monotonic() - parallel_started, "failure" if errors else "success")
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
        "notice": "Research watchlist only. Data may be delayed or incomplete. Not financial advice; no live orders are placed.",
    }


@app.get("/", dependencies=[Depends(require_site_login)])
def home():
    """Open the dashboard on Market Overview by default."""
    return RedirectResponse(url="/history", status_code=302)



APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
PAPER_STATE = APP_DIR / "paper_tracker.json"
PAPER_LIFETIME_STATE = APP_DIR / "paper_tracker_lifetime.json"
BUY_SIGNAL_STATE = APP_DIR / "buy_signal_history.json"
HISTORY_SNAPSHOT = APP_DIR / "history_snapshot.json"
GITHUB_HISTORY_CACHE = {"at": 0.0, "data": None}
GITHUB_HISTORY_CACHE_LOCK = threading.Lock()


def github_history_state() -> dict:
    token = os.getenv("JARVIS_GITHUB_READ_TOKEN", "").strip()
    if not token:
        print("JARVIS_HISTORY_DIAGNOSTIC source=fallback reason=github_read_token_missing", flush=True)
        return {}
    with GITHUB_HISTORY_CACHE_LOCK:
        cached = GITHUB_HISTORY_CACHE.get("data")
        if cached and time.monotonic() - GITHUB_HISTORY_CACHE.get("at", 0.0) < 30:
            return cached
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    base = "https://api.github.com/repos/rileymagiera/jarvis-market-scanner/contents/"
    paths = {"paper_tracker":"paper_tracker.json","lifetime":"paper_tracker_lifetime.json",
             "buy_state":"buy_signal_history.json","metadata":"scan_metadata.json",
             "paper_trading":"paper_trading_state.json"}
    state = {}
    import base64
    for key, path in paths.items():
        try:
            response = HTTP.get(base + path, headers=headers, params={"ref":"main"}, timeout=(3,12))
            response.raise_for_status()
            file_meta = response.json()
            encoded = file_meta.get("content") or ""
            if file_meta.get("encoding") == "base64" and encoded.strip():
                raw_text = base64.b64decode(encoded).decode("utf-8")
            else:
                # GitHub Contents API omits content for larger files. Fetch the blob
                # directly by SHA so the research dashboard can read large histories.
                blob_url = f"https://api.github.com/repos/rileymagiera/jarvis-market-scanner/git/blobs/{file_meta['sha']}"
                blob_response = HTTP.get(blob_url, headers=headers, timeout=(3,20))
                blob_response.raise_for_status()
                blob_meta = blob_response.json()
                if blob_meta.get("encoding") != "base64" or not blob_meta.get("content"):
                    raise ValueError("GitHub blob returned no base64 content")
                raw_text = base64.b64decode(blob_meta["content"]).decode("utf-8")
            state[key] = json.loads(raw_text)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"JARVIS_HISTORY_DIAGNOSTIC source=fallback reason=github_read_failed file={path} status={status or 'unknown'} error_type={type(exc).__name__}", flush=True)
            return {}
    if not isinstance(state.get("paper_tracker"), dict) or not isinstance(state.get("lifetime"), dict):
        raise RuntimeError("GitHub history files returned invalid structure")
    meta = state.get("metadata") or {}
    result = {"paper_tracker":state["paper_tracker"],"lifetime":state["lifetime"],
              "buy_signals":(state.get("buy_state") or {}).get("signals",[]),
              "last_scan_completed_at":meta.get("last_scan_completed_at"),
              "scan_run_id":meta.get("run_id"),"synced_at":meta.get("last_scan_completed_at"),
              "paper_trading":state.get("paper_trading") or {},
              "scan_diagnostics":meta.get("scan_diagnostics") or {}, "history_source":"github"}
    with GITHUB_HISTORY_CACHE_LOCK:
        GITHUB_HISTORY_CACHE["data"] = result
        GITHUB_HISTORY_CACHE["at"] = time.monotonic()
    print("JARVIS_HISTORY_DIAGNOSTIC source=github "
          f"candidates={len(result['paper_tracker'])} "
          f"lifetime_recommendations={result['lifetime'].get('recommendations', 0) if isinstance(result['lifetime'], dict) else 0} "
          f"buy_signals={len(result['buy_signals'])} "
          f"last_scan={result.get('last_scan_completed_at')} run_id={result.get('scan_run_id')}", flush=True)
    return result



def latest_history_state() -> dict:
    """Serve the latest complete Render snapshot immediately; use GitHub only when needed.

    The history page is read frequently. Calling GitHub's Contents API for four files
    before every cold page load made the dashboard wait on several sequential network
    requests even when the cron job had already synced a complete snapshot to Render.
    """
    try:
        snapshot = json.loads(HISTORY_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snapshot = {}
    snapshot_valid = (
        isinstance(snapshot.get("paper_tracker"), dict)
        and isinstance(snapshot.get("lifetime"), dict)
    )
    if snapshot_valid:
        return {
            "paper_tracker": snapshot["paper_tracker"],
            "lifetime": snapshot["lifetime"],
            "buy_signals": snapshot.get("buy_signals", []),
            "last_scan_completed_at": snapshot.get("last_scan_completed_at"),
            "scan_run_id": snapshot.get("scan_run_id"),
            "synced_at": snapshot.get("synced_at"),
            "paper_trading": snapshot.get("paper_trading") or {},
            "scan_diagnostics": snapshot.get("scan_diagnostics") or {},
            "history_source": "render_snapshot_fast",
        }
    return github_history_state()



@app.get("/data-health", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def data_health_page():
    html = (TEMPLATE_DIR / "data_health.html").read_text(encoding="utf-8")
    return apply_workspace_preferences(HTMLResponse(html))


@app.get("/data-health/data", dependencies=[Depends(require_site_login)])
def data_health_data():
    state = latest_history_state()
    records = state.get("paper_tracker", {}) if isinstance(state, dict) else {}
    records = records if isinstance(records, dict) else {}
    candidates = [item for item in records.values() if isinstance(item, dict) and item.get("currently_surfaced") is not False]
    diagnostics = state.get("scan_diagnostics", {}) if isinstance(state, dict) else {}
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    detail = diagnostics.get("diagnostics") if isinstance(diagnostics.get("diagnostics"), dict) else diagnostics
    stock = [x for x in candidates if x.get("asset_type") == "stock"]
    crypto = [x for x in candidates if x.get("asset_type") == "crypto"]
    def status_counts(items):
        result = {"verified": 0, "last_known": 0, "unverified": 0, "unknown": 0}
        for item in items:
            status = str(item.get("price_validation_status") or "").lower()
            result[status if status in result else "unknown"] += 1
        return result
    def coverage(items, keys):
        return {key: sum(1 for item in items if item.get(key) is not None) for key in keys}
    def validation_reason(item):
        note = str(item.get("price_validation_note") or "").strip()
        return note or "No validation explanation was saved"
    unverified_candidates = [item for item in candidates
                             if str(item.get("price_validation_status") or "").lower() == "unverified"]
    reason_counts = {}
    for item in unverified_candidates:
        reason = validation_reason(item)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    failure_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))
    ]
    def quality(item):
        value = item.get("data_quality")
        return value if isinstance(value, dict) else {}
    missing_price = [x for x in candidates if safe_float(x.get("latest_price") or x.get("price"), 0) <= 0]
    missing_source = [x for x in candidates if not (x.get("price_source") or quality(x).get("data_source"))]
    missing_timestamp = [x for x in candidates if not (x.get("price_as_of") or quality(x).get("observed_at"))]
    stale = [x for x in candidates if quality(x).get("age_seconds") is not None and safe_float(quality(x).get("age_seconds")) > 900]
    alpaca = detail.get("alpaca_quote_status", {}) if isinstance(detail, dict) else {}
    crypto_diag = detail.get("crypto", {}) if isinstance(detail, dict) else {}
    return {
        "last_scan_completed_at": state.get("last_scan_completed_at") if isinstance(state, dict) else None,
        "scan_run_id": state.get("scan_run_id") if isinstance(state, dict) else None,
        "history_source": state.get("history_source", "unknown") if isinstance(state, dict) else "unknown",
        "candidate_count": len(candidates), "assets": {"stocks": len(stock), "crypto": len(crypto)},
        "price_status": {"all": status_counts(candidates), "stocks": status_counts(stock), "crypto": status_counts(crypto)},
        "failure_reasons": failure_reasons,
        "webull_yahoo_comparison": detail.get("webull_yahoo_comparison") or {},
        "crypto_shadow_comparison": detail.get("crypto_shadow_comparison") or {},
        "data_gaps": {"missing_price": len(missing_price), "missing_source": len(missing_source),
                      "missing_timestamp": len(missing_timestamp), "older_than_15_minutes": len(stale)},
        "field_coverage": {
            "stocks": coverage(stock, ("data_quality", "daily_data_quality", "intraday_data_quality", "float_shares", "shares_outstanding", "spread_pct", "catalyst_headline_count")),
            "crypto": coverage(crypto, ("data_quality", "hourly_data_quality", "crqs", "crqs_data_coverage", "catalyst_headline_count")),
        },
        "providers": {
            "stock_price_feed": alpaca.get("feed"), "alpaca_credentials_configured": alpaca.get("credentials_configured"),
            "alpaca_candidate_status_counts": alpaca.get("candidate_status_counts", {}),
            "webull_status": detail.get("webull_quote_status"), "webull_note": detail.get("webull_quote_note"),
            "webull_shadow_credentials_configured": (detail.get("webull_shadow_sample") or {}).get("credentials_configured", False),
            "massive_status": detail.get("massive_live_quote_status"), "massive_note": detail.get("massive_live_quote_note"),
            "crypto_verified": (crypto_diag.get("price_validation") or {}).get("verified"),
            "crypto_unverified": (crypto_diag.get("price_validation") or {}).get("unverified"),
        },
        "scan_errors": (detail.get("stocks", {}).get("scan_errors", 0) + detail.get("crypto", {}).get("scan_errors", 0)) if isinstance(detail, dict) else 0,
        "provider_http_429_count": detail.get("timing_breakdown", {}).get("provider_activity", {}).get("provider_http_429_count", 0) if isinstance(detail, dict) else 0,
        "provider_http_5xx_count": detail.get("timing_breakdown", {}).get("provider_activity", {}).get("provider_http_5xx_count", 0) if isinstance(detail, dict) else 0,
        "candidates": [{
            "symbol": item.get("symbol") or "Unknown", "asset_type": item.get("asset_type") or "unknown",
            "stage": item.get("last_stage") or item.get("stage") or "Not recorded",
            "price_status": item.get("price_validation_status") or "unknown",
            "validation_note": item.get("price_validation_note") or "No validation explanation was saved",
            "failure_reason": validation_reason(item) if str(item.get("price_validation_status") or "").lower() == "unverified" else None,
            "price": item.get("latest_price") or item.get("price"), "source": item.get("price_source") or quality(item).get("data_source"),
            "observed_at": item.get("price_as_of") or quality(item).get("observed_at"),
            "age_seconds": quality(item).get("age_seconds"), "data_type": quality(item).get("data_type") or "unknown",
            "buy_eligible": quality(item).get("buy_eligible"),
            "missing": [label for label, value in (("price", item.get("latest_price") or item.get("price")),
                ("source", item.get("price_source") or quality(item).get("data_source")),
                ("timestamp", item.get("price_as_of") or quality(item).get("observed_at"))) if value is None or value == ""]
        } for item in sorted(candidates, key=lambda x: (0 if x.get("price_validation_status") == "unverified" else 1 if x.get("price_validation_status") == "last_known" else 2, str(x.get("symbol") or "")))[:250]]
    }


@app.get("/intelligence/audit-export", dependencies=[Depends(require_site_login)])
@app.get("/research/audit-export", dependencies=[Depends(require_site_login)])
def historical_audit_export(symbols: str | None = None):
    """Bounded read-only JSON export of retained missed-runner and BUY-gate research."""
    state = latest_history_state()
    if not isinstance(state, dict) or not isinstance(state.get("paper_tracker"), dict):
        raise HTTPException(status_code=503, detail="Historical candidate data is not currently available")
    records = [row for row in state["paper_tracker"].values() if isinstance(row, dict)]
    try:
        return build_historical_audit_export(
            records,
            state.get("buy_signals") if isinstance(state.get("buy_signals"), list) else [],
            last_scan_completed_at=state.get("last_scan_completed_at"),
            scan_run_id=state.get("scan_run_id"),
            synced_at=state.get("synced_at"),
            history_source=state.get("history_source", "unknown"),
            requested_symbols=symbols,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/history", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def history_page():
    html = inject_mobile_navigation((TEMPLATE_DIR / "history.html").read_text(encoding="utf-8"))
    return HTMLResponse(html, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"})
def analytics_tab_page(active: str) -> str:
    tabs = [
        ("data-health", "Data Health"),
        ("discovery-performance", "Discovery Performance"),
        ("intelligence", "Intelligence"),
        ("market-breadth", "Market Breadth"),
        ("history", "Market Overview"),
        ("momentum-evidence", "Momentum Evidence"),
        ("paper-trading", "Paper Trading"),
        ("settings", "Settings"),
    ]
    nav = "".join(
        f'<a class="{"active" if slug == active else ""}" href="/{slug}">{label}</a>'
        for slug, label in tabs
    )
    titles = {
        "discovery-performance": ("DISCOVERY PERFORMANCE", "Discovery Performance",
            "Track what happened after Jarvis first discovered each candidate."),
        "momentum-evidence": ("MOMENTUM EVIDENCE", "Momentum Evidence",
            "Compare current price momentum, volume participation, relative strength and trend evidence."),
        "market-breadth": ("MARKET BREADTH", "Market Breadth",
            "See whether the currently scanned stock cohort is broadly participating. This is not full-exchange breadth."),
    }
    eyebrow, title, subtitle = titles[active]
    page = (TEMPLATE_DIR / "analytics_tab.html").read_text(encoding="utf-8")
    return (page.replace("__TITLE__", title)
                .replace("__SUBTITLE__", subtitle)
                .replace("__NAV__", nav)
                .replace("__KIND__", active)
                .replace("__HEATMAP_DISPLAY__", "block" if active == "market-breadth" else "none"))


def customize_page() -> str:
    return (TEMPLATE_DIR / "settings.html").read_text(encoding="utf-8")



def inject_mobile_navigation(html: str) -> str:
    """Compact mobile shortcuts without removing any of the desktop's eight pages."""
    if 'id="jarvis-mobile-navigation"' in html:
        return html
    match = re.search(r"<nav\b[^>]*>[\s\S]*?</nav>", html)
    if match is None:
        return html
    desktop = match.group(0)
    active = None
    for anchor in re.findall(r"<a\b[^>]*>[^<]*</a>", desktop):
        href = re.search(r'href="/([^"]+)"', anchor)
        if href and ('aria-current="page"' in anchor or re.search(r'class="[^"]*\bactive\b', anchor)):
            active = href.group(1)
            break
    primary = [("history", "Market Overview"), ("intelligence", "Intelligence"),
               ("paper-trading", "Paper Trading")]
    secondary = [("data-health", "Data Health"),
                 ("discovery-performance", "Discovery Performance"),
                 ("market-breadth", "Market Breadth"),
                 ("momentum-evidence", "Momentum Evidence"), ("settings", "Settings")]

    def link(slug: str, label: str) -> str:
        current = ' aria-current="page"' if active == slug else ""
        return f'<a href="/{slug}"{current}>{label}</a>'

    extra_label = next((label for slug, label in secondary if slug == active), None)
    more_label = "More" if not extra_label else "More · " + extra_label
    mobile = (
        '<nav id="jarvis-mobile-navigation" class="jarvis-mobile-nav" aria-label="Primary mobile navigation">'
        + "".join(link(slug, label) for slug, label in primary)
        + '<details class="jarvis-mobile-more"><summary>'
        + more_label + '</summary><div class="jarvis-mobile-more-links">'
        + "".join(link(slug, label) for slug, label in secondary)
        + '</div></details></nav>'
    )
    style = """<style id="jarvis-mobile-navigation-style">
.jarvis-mobile-nav{display:none}
@media(max-width:700px){
  nav.top-tabs,nav.tabs{display:none!important}
  .jarvis-mobile-nav{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:13px 0 19px;position:relative;z-index:25}
  .jarvis-mobile-nav>a,.jarvis-mobile-more>summary{display:flex;align-items:center;justify-content:center;min-height:48px;padding:7px 5px;border:1px solid #3b5268;border-radius:11px;background:#162839;color:#dce9f5;text-decoration:none;text-align:center;font:700 11px/1.3 system-ui,sans-serif;overflow-wrap:anywhere;cursor:pointer}
  .jarvis-mobile-nav a[aria-current="page"],.jarvis-mobile-more[open]>summary{border-color:#73c9bd;background:#214342;color:#ecfffa}
  .jarvis-mobile-nav a:focus-visible,.jarvis-mobile-more summary:focus-visible{outline:2px solid #92dbfa;outline-offset:2px}
  .jarvis-mobile-more{position:relative;min-width:0}
  .jarvis-mobile-more>summary{list-style:none;height:100%}
  .jarvis-mobile-more>summary::-webkit-details-marker{display:none}
  .jarvis-mobile-more-links{position:absolute;right:0;top:calc(100% + 6px);width:min(270px,calc(100vw - 28px));padding:7px;background:#0d1d2c;border:1px solid #486378;border-radius:12px;box-shadow:0 12px 26px #0009;display:grid;gap:4px;z-index:50}
  .jarvis-mobile-more-links a{display:block;padding:12px 11px;min-height:43px;border-radius:8px;color:#e4f0fa;text-decoration:none;font:600 13px/1.4 system-ui,sans-serif}
  .jarvis-mobile-more-links a:hover,.jarvis-mobile-more-links a[aria-current="page"]{background:#254a4b}
}
</style>"""
    updated = html[:match.end()] + mobile + html[match.end():]
    return updated.replace("</head>", style + "</head>", 1)


def apply_workspace_preferences(response: HTMLResponse) -> HTMLResponse:
    """Apply browser-saved appearance preferences consistently across Jarvis pages."""
    if not isinstance(response, HTMLResponse):
        return response
    injection = """<style id="jarvis-workspace-preferences">
.tabs a.active,.tabs a[aria-current="page"]{border-color:var(--accent)!important}
.eyebrow{color:var(--accent)!important}
body.jarvis-compact .hero,body.jarvis-compact .panel{padding:12px!important}
body.jarvis-compact .section{margin-top:16px!important}
</style><script>
(()=>{const key='jarvisDashboardPrefsV1';let p={};try{p=JSON.parse(localStorage.getItem(key)||'{}')||{}}catch(_){}
if(/^#[0-9a-f]{6}$/i.test(String(p.accent||'')))document.documentElement.style.setProperty('--accent',p.accent);
if(typeof p.font==='string'&&p.font.length<100)document.body.style.fontFamily=p.font;
const size=Number(p.fontSize);if(Number.isFinite(size)&&size>=13&&size<=16)document.body.style.fontSize=size+'px';
document.body.classList.toggle('jarvis-compact',p.density==='compact');
})();
</script>"""
    try:
        html = inject_mobile_navigation(response.body.decode("utf-8"))
        if "</body>" in html and "jarvis-workspace-preferences" not in html:
            response.body = html.replace("</body>", injection + "</body>", 1).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
    except (AttributeError, UnicodeDecodeError):
        return response
    return response

@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
@app.get("/customize", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def customize():
    return HTMLResponse(inject_mobile_navigation(customize_page()), headers={"Cache-Control":"no-store"})
    
@app.get("/discovery-performance", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def discovery_performance_page():
    return apply_workspace_preferences(HTMLResponse(analytics_tab_page("discovery-performance")))


@app.get("/momentum-evidence", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def momentum_evidence_page():
    return apply_workspace_preferences(HTMLResponse(analytics_tab_page("momentum-evidence")))


@app.get("/market-breadth", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def market_breadth_page():
    return apply_workspace_preferences(HTMLResponse(analytics_tab_page("market-breadth")))


@app.get("/history/dashboard.js", dependencies=[Depends(require_site_login)])
def history_dashboard_js():
    # Serve the dashboard code separately so browser parse/load failures are visible
    # as a distinct request and cannot be hidden inside the large HTML response.
    dashboard_js = (Path(__file__).resolve().parent / "static" / "history_dashboard.js").read_text(encoding="utf-8")
    return Response(content=dashboard_js, media_type="application/javascript",
                    headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/history/data", dependencies=[Depends(require_site_login)])
def history_data():
    state = latest_history_state()
    if state:
        return {"last_scan_completed_at": state.get("last_scan_completed_at"),
                "scan_run_id": state.get("scan_run_id"), "paper_tracker": state.get("paper_tracker", {}),
                "lifetime": state.get("lifetime", {}), "buy_signals": state.get("buy_signals", []),
                "scan_diagnostics": state.get("scan_diagnostics", {}),
                "history_source": state.get("history_source", "github")}
    def read_state(path):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {}
    paper=read_state(PAPER_STATE); lifetime=read_state(PAPER_LIFETIME_STATE)
    buys=read_state(BUY_SIGNAL_STATE).get("signals", []); meta=read_state(APP_DIR/"scan_metadata.json")
    return {"last_scan_completed_at": meta.get("last_scan_completed_at"), "scan_run_id": meta.get("run_id"),
            "paper_tracker": paper, "lifetime": lifetime, "buy_signals": buys,
            "scan_diagnostics": meta.get("scan_diagnostics") or {}, "history_source": "local_fallback"}


def apply_discovery_baseline_guard(payload: dict) -> dict:
    """Preserve discovery baselines across syncs and capture only reliable timestamped fallbacks.

    The original first_seen timestamp is never rewritten. A fallback baseline is explicitly
    marked as delayed so it is not confused with the original discovery-time quote.
    """
    paper = payload.get("paper_tracker")
    if not isinstance(paper, dict):
        return payload

    previous = {}
    try:
        snapshot = json.loads(HISTORY_SNAPSHOT.read_text(encoding="utf-8"))
        if isinstance(snapshot.get("paper_tracker"), dict):
            previous = snapshot["paper_tracker"]
    except (OSError, json.JSONDecodeError):
        previous = {}

    baseline_fields = (
        "discovery_initial_price",
        "discovery_price_as_of",
        "discovery_price_checked_at",
        "discovery_price_source",
        "discovery_baseline_timestamp_type",
        "discovery_baseline_status",
        "discovery_baseline_captured_at",
        "discovery_baseline_source",
    )
    history_fields = ("discovery_return_pct", "discovery_max_gain_pct", "discovery_max_drawdown_pct")
    captured_at = now_iso()
    for key, record in paper.items():
        if not isinstance(record, dict):
            continue

        # GitHub scanner history is authoritative when it carries a baseline.
        # Render's previous cached snapshot is only a fallback for missing fields;
        # overwriting an incoming first-captured price can corrupt returns.
        old = previous.get(key) if isinstance(previous, dict) else None
        if isinstance(old, dict):
            old_price = safe_float(old.get("discovery_initial_price"), 0.0)
            current_price = safe_float(record.get("discovery_initial_price"), 0.0)
            if old_price > 0 and current_price <= 0:
                for field in baseline_fields:
                    if field in old:
                        record[field] = old[field]
            for field in history_fields:
                if record.get(field) is None and field in old:
                    record[field] = old[field]

        baseline = safe_float(record.get("discovery_initial_price"), 0.0)
        if baseline > 0:
            status = str(record.get("price_validation_status") or "").strip().lower()
            latest = safe_float(record.get("latest_price"), 0.0)
            if status in ("verified", "last_known") and latest > 0:
                record["discovery_return_pct"] = round((latest / baseline - 1.0) * 100.0, 6)
                # A verified quote can still be delayed. Mark such radar returns
                # as provisional; live outcome sampling remains scanner-controlled.
                quality = record.get("data_quality")
                price_type = str(quality.get("data_type") or "").lower() if isinstance(quality, dict) else ""
                if status == "last_known" or price_type in {"delayed", "historical", "unknown"}:
                    record["discovery_return_status"] = "provisional"
                record["discovery_return_price_as_of"] = record.get("price_as_of")
            continue

        # Never establish a baseline from an unverified quote or an untimestamped value.
        status = str(record.get("price_validation_status") or "").strip().lower()
        if status not in ("verified", "last_known"):
            continue
        price = safe_float(record.get("latest_price"), 0.0)
        as_of = record.get("price_as_of")
        if price <= 0 or not as_of:
            continue
        try:
            stamp = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            if stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                continue
        except (TypeError, ValueError):
            continue

        record["discovery_initial_price"] = price
        record["discovery_price_as_of"] = str(as_of)
        record["discovery_baseline_status"] = "first_reliable_fallback"
        record["discovery_baseline_captured_at"] = captured_at
        record["discovery_baseline_source"] = record.get("price_source")
        record["discovery_return_pct"] = 0.0
    return payload


# The history-sync route accepts both the original JSON and a losslessly
# compressed representation. All existing callers and state semantics remain.
MAX_HISTORY_SYNC_BYTES = 128 * 1024 * 1024


def decode_history_sync_payload(raw: bytes, encoding: str = "") -> dict:
    if len(raw) > MAX_HISTORY_SYNC_BYTES:
        raise HTTPException(status_code=413, detail="History payload too large")
    if encoding.strip().lower() == "gzip":
        try:
            # Read a bounded amount to reject oversized decompression safely.
            with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
                raw = stream.read(MAX_HISTORY_SYNC_BYTES + 1)
        except (OSError, EOFError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid gzip history payload") from exc
    elif encoding.strip().lower() not in ("", "identity"):
        raise HTTPException(status_code=415, detail="Unsupported history content encoding")
    if len(raw) > MAX_HISTORY_SYNC_BYTES:
        raise HTTPException(status_code=413, detail="History payload too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid history JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid history payload")
    return payload


def history_sync(payload: dict, x_jarvis_history_token: Optional[str] = Header(None)):
    expected=os.getenv("JARVIS_HISTORY_SYNC_TOKEN","")
    if not expected or not x_jarvis_history_token or not hmac.compare_digest(expected,x_jarvis_history_token):
        raise HTTPException(status_code=401,detail="Unauthorized")
    if not isinstance(payload.get("paper_tracker"),dict) or not isinstance(payload.get("lifetime"),dict):
        raise HTTPException(status_code=400,detail="Invalid history payload")
    payload = apply_discovery_baseline_guard(payload)
    HISTORY_SNAPSHOT.write_text(json.dumps(payload,separators=(",",":")),encoding="utf-8")
    with GITHUB_HISTORY_CACHE_LOCK:
        GITHUB_HISTORY_CACHE["data"]=None; GITHUB_HISTORY_CACHE["at"]=0.0
    return {"status":"synced","records":len(payload["paper_tracker"])}


@app.post("/history/sync")
async def history_sync_endpoint(request: Request, x_jarvis_history_token: Optional[str] = Header(None)):
    expected = os.getenv("JARVIS_HISTORY_SYNC_TOKEN", "")
    if not expected or not x_jarvis_history_token or not hmac.compare_digest(expected, x_jarvis_history_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_history_sync_payload(
        await request.body(), request.headers.get("content-encoding", "")
    )
    return history_sync(payload, x_jarvis_history_token=x_jarvis_history_token)


@app.get("/paper-trading", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def paper_trading_page():
    html = (TEMPLATE_DIR / "paper_trading.html").read_text(encoding="utf-8")
    return apply_workspace_preferences(HTMLResponse(html))

@app.get("/paper-trading/data", dependencies=[Depends(require_site_login)])
def paper_trading_data():
    state = latest_history_state()
    account = state.get("paper_trading") if isinstance(state, dict) else None
    if not isinstance(account, dict) or account.get("version") != 1:
        account = load_paper_account()
    return {"account": account, "summary": summarize_paper_account(account),
            "source": state.get("history_source", "local_default") if isinstance(account, dict) else "local_default"}


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": now_iso()}


@app.get("/symbols", dependencies=[Depends(require_site_login)])
def symbols():
    return {
        "stocks": stock_universe(),
        "crypto": crypto_universe(),
    }


def premarket_provider_audit(item: dict, checked_at=None) -> dict:
    """Classify extended-hours source freshness without claiming unverified feeds are live."""
    checked_at = checked_at or datetime.now(timezone.utc)
    yahoo_as_of = item.get("intraday_as_of")
    yahoo_age_seconds = None
    try:
        yahoo_stamp = pd.Timestamp(yahoo_as_of)
        if yahoo_stamp.tzinfo is None:
            yahoo_stamp = yahoo_stamp.tz_localize("UTC")
        yahoo_age_seconds = (checked_at - yahoo_stamp.tz_convert("UTC").to_pydatetime()).total_seconds()
    except (TypeError, ValueError, OverflowError):
        pass
    yahoo_recent = bool(
        item.get("intraday_price") and yahoo_age_seconds is not None
        and -300 <= yahoo_age_seconds <= 300
    )
    alpaca_price = safe_float(item.get("alpaca_price"), 0)
    alpaca_age = item.get("alpaca_price_age_minutes")
    alpaca_feed = str(item.get("alpaca_feed") or "").lower()
    alpaca_fresh = bool(
        alpaca_price > 0 and alpaca_age is not None
        and -1 <= safe_float(alpaca_age, 9999) <= 5
    )
    return {
        "yahoo": {
            "price": item.get("intraday_price") or None,
            "as_of": yahoo_as_of,
            "age_seconds": round(yahoo_age_seconds, 1) if yahoo_age_seconds is not None else None,
            "status": "RECENT — ENTITLEMENT UNVERIFIED" if yahoo_recent else "STALE / UNAVAILABLE",
            "counts_as_live": False,
        },
        "alpaca": {
            "price": alpaca_price or None,
            "as_of": item.get("alpaca_price_as_of"),
            "age_minutes": alpaca_age,
            "feed": alpaca_feed or None,
            "status": "FRESH IEX/SIP QUOTE" if alpaca_fresh and alpaca_feed in {"iex", "sip"} else "STALE / UNAVAILABLE",
            "coverage_note": "IEX is exchange-limited; SIP coverage depends on feed entitlement",
            "counts_as_consolidated_live": bool(alpaca_fresh and alpaca_feed == "sip"),
        },
    }


@app.get("/scan/premarket", dependencies=[Depends(require_site_login)])
def scan_premarket(limit: int = Query(150, ge=20, le=200)):
    """Dedicated research-only premarket early-runner ranking; does not change BUY gates."""
    scan_started = time.monotonic()
    reset_scan_metrics()
    universe = stock_universe()[:limit]
    prefetch_alpaca_snapshots(universe)
    results, errors = run_parallel(universe, scan_stock, workers=6)
    session = market_session_status()
    ranked = []
    for item in results:
        gap = safe_float(item.get("premarket_gap_pct"), 0.0)
        pre_volume = safe_float(item.get("premarket_volume"), 0.0)
        pre_rvol = safe_float(item.get("premarket_rvol_same_time"), 0.0)
        # Broad discovery criteria; the displayed quote is labeled by its source/freshness below.
        if gap < 2.5 and not (pre_volume >= 25000 and pre_rvol >= 1.25):
            continue
        catalyst = bool(item.get("catalyst_labels"))
        runner_score = safe_float(item.get("early_runner_research_score"), 0.0)
        score = min(100, max(0, round(
            min(max(gap, 0), 25) * 1.2
            + min(pre_rvol, 10) * 4
            + min(pre_volume / 10000, 10) * 1.0
            + runner_score * 0.25
            + (12 if catalyst else 0)
            - (15 if item.get("dilution_flags") else 0)
        )))
        item["premarket_rank_score"] = score
        item["premarket_alert_status"] = (
            "PREMARKET VERIFIED" if item.get("price_validation_passed") and item.get("data_quality", {}).get("verification_status") == "verified"
            else "PREMARKET PROVISIONAL — SINGLE / UNCONFIRMED SOURCE"
        )
        item["premarket_provider_checks"] = premarket_provider_audit(item)
        item["premarket_quote"] = {
            "price": item.get("intraday_price") if item.get("intraday_price") else None,
            "as_of": item.get("intraday_as_of"),
            "source": "Yahoo Finance extended-hours intraday bars",
            "fresh": item["premarket_provider_checks"]["yahoo"]["status"].startsWith("RECENT"),
            "verification": "PROVISIONAL — Yahoo delay entitlement unverified",
        }
        item["premarket_reasons"] = [
            f"Gap {gap:+.2f}%" if item.get("premarket_gap_pct") is not None else None,
            f"Premarket RVOL {pre_rvol:.2f}x" if pre_rvol else None,
            f"Premarket volume {pre_volume:,.0f}" if pre_volume else None,
            "Headline catalyst detected" if catalyst else None,
            "Financing/dilution risk flagged" if item.get("dilution_flags") else None,
        ]
        item["premarket_reasons"] = [x for x in item["premarket_reasons"] if x]
        ranked.append(item)
    ranked.sort(key=lambda x: x.get("premarket_rank_score", 0), reverse=True)
    provider_audit = {
        "yahoo_recent_unverified": sum(1 for x in ranked if x["premarket_provider_checks"]["yahoo"]["status"].startswith("RECENT")),
        "alpaca_fresh_iex_or_sip": sum(1 for x in ranked if x["premarket_provider_checks"]["alpaca"]["status"] == "FRESH IEX/SIP QUOTE"),
        "alpaca_consolidated_sip": sum(1 for x in ranked if x["premarket_provider_checks"]["alpaca"]["counts_as_consolidated_live"]),
        "note": "Yahoo bars are not counted as live because delay entitlement is unverified. Alpaca IEX is exchange-limited; SIP is counted as consolidated only when the configured feed is SIP.",
    }
    return {
        "scanner": "Jarvis Premarket Early Runner",
        "version": "1.0",
        "timestamp": now_iso(),
        "market_session": session,
        "symbols_requested": len(universe),
        "candidates_found": len(ranked),
        "provider_audit": provider_audit,
        "candidates": ranked[:50],
        "errors": errors,
        "duration_seconds": round(time.monotonic() - scan_started, 2),
        "notice": "Research-only premarket ranking. Quotes may be provisional, delayed, or stale. This endpoint never changes regular BUY eligibility or places trades.",
    }

@app.get("/scan/stocks", dependencies=[Depends(require_site_login)])
def scan_stocks(limit: int = Query(30, ge=1, le=100)):
    universe = stock_universe()[:limit]
    reset_scan_metrics()
    prefetch_alpaca_snapshots(universe)
    results, errors = run_parallel(universe, scan_stock)
    return response_payload("stocks", universe, results, errors)


@app.get("/scan/crypto", dependencies=[Depends(require_site_login)])
def scan_cryptocurrencies(limit: int = Query(25, ge=1, le=100)):
    universe = crypto_universe()[:limit]
    results, errors = run_parallel(universe, scan_crypto)
    return response_payload("crypto", universe, results, errors)


@app.get("/scan", dependencies=[Depends(require_site_login)])
def scan(asset: str = Query("all", pattern="^(all|stocks|crypto)$")):
    scan_started = time.monotonic()
    reset_scan_metrics()
    discovery_started = time.monotonic()
    stock_symbols = stock_universe() if asset in ("all", "stocks") else []
    crypto_symbols = crypto_universe() if asset in ("all", "crypto") else []
    discovery_seconds = time.monotonic() - discovery_started
    global WEBULL_SHADOW_SYMBOLS
    webull_credentials_configured = bool(os.getenv("WEBULL_APP_KEY", "").strip() and os.getenv("WEBULL_APP_SECRET", "").strip())
    WEBULL_SHADOW_SYMBOLS = set(
        select_webull_shadow_symbols(stock_symbols, os.getenv("GITHUB_RUN_NUMBER"))
        if WEBULL_SHADOW_MODE and webull_credentials_configured else []
    )
    stock_results, stock_errors = ([], [])
    crypto_results, crypto_errors = ([], [])

    # Pull snapshots in 50-symbol batches before workers start, replacing the
    # old per-symbol Alpaca quote requests and making coverage consistent.
    if stock_symbols:
        prefetch_alpaca_snapshots(stock_symbols)

    # Run independent asset classes together, with a bounded worker pool per
    # class. This prevents the crypto pass from waiting for every stock request.
    if asset == "all":
        with ThreadPoolExecutor(max_workers=2) as pool:
            stock_future = pool.submit(run_parallel, stock_symbols, scan_stock, 4)
            crypto_future = pool.submit(run_parallel, crypto_symbols, scan_crypto, 4)
            stock_results, stock_errors = stock_future.result()
            crypto_results, crypto_errors = crypto_future.result()
    elif asset == "stocks":
        stock_results, stock_errors = run_parallel(stock_symbols, scan_stock)
    elif asset == "crypto":
        crypto_results, crypto_errors = run_parallel(crypto_symbols, scan_crypto)

    asset_scans_seconds = time.monotonic() - scan_started - discovery_seconds
    results = sorted(stock_results + crypto_results, key=lambda item: item["score"], reverse=True)
    errors = stock_errors + crypto_errors
    market_started = time.monotonic()
    regime = market_conditions()
    market_conditions_seconds = time.monotonic() - market_started
    record_scan_metric("market_conditions", market_conditions_seconds)
    benchmarks = (regime.get("benchmarks") or {})
    stock_items = [x for x in results if x.get("asset_type") == "stock"]
    for item in stock_items:
        five_day = safe_float(item.get("five_day_change_pct"))
        spy = safe_float((benchmarks.get("SPY") or {}).get("five_day_change_pct"), np.nan)
        iwm = safe_float((benchmarks.get("IWM") or {}).get("five_day_change_pct"), np.nan)
        item["relative_strength_vs_spy"] = round(five_day-spy, 2) if np.isfinite(spy) else None
        item["relative_strength_vs_iwm"] = round(five_day-iwm, 2) if np.isfinite(iwm) else None
    ranked_rs = sorted((x for x in stock_items if x.get("relative_strength_vs_spy") is not None),
                       key=lambda x: x["relative_strength_vs_spy"])
    for rank, item in enumerate(ranked_rs, 1):
        item["rs_rank"] = round(rank/len(ranked_rs)*100, 1) if ranked_rs else None
    # Additive stock-universe context; research-only and never changes BUY gates.
    stock_universe_strength = rank_stock_universe_strength(stock_items)
    stock_market_breadth = compute_stock_market_breadth(stock_items)
    for item in stock_items:
        item["market_breadth"] = stock_market_breadth
    # Crypto cross-sectional intelligence uses Kraken's full ticker universe,
    # not the filtered list of symbols that happened to qualify as candidates.
    crypto_items = [x for x in results if x.get("asset_type") == "crypto"]
    crypto_sectors = {
        "BTCUSDT":"Bitcoin", "ETHUSDT":"Ethereum", "SOLUSDT":"Layer 1", "BNBUSDT":"Layer 1", "AVAXUSDT":"Layer 1", "ADAUSDT":"Layer 1", "DOTUSDT":"Layer 1", "SUIUSDT":"Layer 1", "SEIUSDT":"Layer 1", "APTUSDT":"Layer 1", "NEARUSDT":"Layer 1",
        "ARBUSDT":"Layer 2", "OPUSDT":"Layer 2", "POLUSDT":"Layer 2",
        "LINKUSDT":"Oracle / Infrastructure", "RENDERUSDT":"AI / Compute", "FETUSDT":"AI / Compute", "TAOUSDT":"AI / Compute",
        "JUPUSDT":"DeFi", "INJUSDT":"DeFi", "ONDOUSDT":"RWA",
        "DOGEUSDT":"Memecoin", "WIFUSDT":"Memecoin", "PEPEUSDT":"Memecoin", "BONKUSDT":"Memecoin",
        "XRPUSDT":"Payments", "TRXUSDT":"Payments", "TONUSDT":"Consumer / Social", "XLMUSDT":"Payments", "ZECUSDT":"Privacy",
    }
    try:
        crypto_cross_section = kraken_crypto_cross_section(crypto_sectors)
    except Exception as exc:
        crypto_cross_section = {"btc_change_pct": None, "btc_source": None, "sector_stats": {}}
        print(f"CRYPTO_CROSS_SECTION unavailable: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
    btc_change = crypto_cross_section.get("btc_change_pct")
    sector_stats = crypto_cross_section.get("sector_stats") or {}
    # Candidate-derived fallback is diagnostic only; it cannot earn sector points.
    if not sector_stats:
        fallback_members = {}
        for candidate in crypto_items:
            symbol = candidate.get("symbol", "").upper()
            sector = crypto_sectors.get(symbol, "Other")
            fallback_members.setdefault(sector, []).append(candidate)
        for sector, members in fallback_members.items():
            values = [safe_float(x.get("daily_change_pct"), np.nan) for x in members]
            values = [v for v in values if np.isfinite(v)]
            sector_stats[sector] = {
                "avg_24h_change_pct": round(float(np.mean(values)), 2) if values else None,
                "positive_breadth_pct": round(sum(v > 0 for v in values) / len(values) * 100, 1) if values else None,
                "sample_size": len(values), "universe_size": None, "coverage_pct": None,
                "reliable": False, "source": "candidate-only fallback; insufficient for sector scoring",
            }
    ranked_sectors = sorted(
        (sector for sector, stats in sector_stats.items() if stats.get("reliable")),
        key=lambda sector: safe_float(sector_stats[sector].get("avg_24h_change_pct"), -999),
        reverse=True,
    )
    for crypto in crypto_items:
        sector = crypto_sectors.get(crypto.get("symbol", "").upper(), "Other")
        stats = sector_stats.get(sector, {"sample_size": 0, "reliable": False, "source": "no sector benchmark"})
        relative = round(safe_float(crypto.get("daily_change_pct")) - btc_change, 2) if btc_change is not None else None
        rs_points = 15 if relative is not None and relative >= 5 else 10 if relative is not None and relative >= 2 else 5 if relative is not None and relative > 0 else 0
        sector_avg = stats.get("avg_24h_change_pct")
        sector_breadth = stats.get("positive_breadth_pct")
        sector_points = 0
        if stats.get("reliable"):
            sector_points = 10 if sector_avg is not None and sector_avg >= 3 and sector_breadth is not None and sector_breadth >= 60 else 5 if sector_avg is not None and sector_avg > 0 and sector_breadth is not None and sector_breadth >= 50 else 0
        components = crypto.get("crqs_components") or {}
        components["relative_strength"] = rs_points
        components["sector_activity"] = sector_points
        crypto["crqs_components"] = components
        crypto["crqs_relative_strength_vs_btc_24h_pct"] = relative
        crypto["crqs_sector_activity"] = stats
        crypto["crqs_sector_rank"] = ranked_sectors.index(sector) + 1 if sector in ranked_sectors else None
        crypto["crqs_sector_count"] = len(ranked_sectors)
        crypto["crqs_data_coverage"] = crypto.get("crqs_data_coverage") or {}
        set_crypto_relative_strength_coverage(crypto["crqs_data_coverage"], relative)
        crypto["crqs_data_coverage"]["sector_activity"] = bool(stats.get("reliable"))
        crypto["crqs_data_coverage"]["sector_activity_sample_size"] = stats.get("sample_size", 0)
        crypto["crqs_data_coverage"]["sector_activity_coverage_pct"] = stats.get("coverage_pct")
        crypto["crqs"] = min(74, sum(safe_float(v) for v in components.values()))
        penalty = safe_float(crypto.get("crqs_entry_penalty"))
        crypto["crqs_entry_score"] = max(0, min(74, crypto["crqs"] - penalty))
        score = crypto["crqs"]
        crypto["crqs_tier"] = "Elite Runner" if score >= 65 else "High Conviction" if score >= 58 else "Developing Runner" if score >= 50 else "Early Discovery" if score >= 42 else "Watch" if score >= 32 else "Low Quality"
        crypto["crqs_features"] = crypto.get("crqs_features") or {}
        crypto["crqs_features"]["btc_24h_change_pct"] = btc_change
        crypto["crqs_features"]["btc_24h_benchmark_source"] = crypto_cross_section.get("btc_source")
        crypto["crqs_features"]["crypto_sector"] = sector
        crypto["crqs_features"]["sector_avg_24h_change_pct"] = stats.get("avg_24h_change_pct")
        crypto["crqs_features"]["sector_positive_breadth_pct"] = stats.get("positive_breadth_pct")
        crypto["crqs_features"]["sector_sample_size"] = stats.get("sample_size")
        crypto["crqs_features"]["sector_universe_size"] = stats.get("universe_size")
        crypto["crqs_features"]["sector_coverage_pct"] = stats.get("coverage_pct")
        crypto["crqs_features"]["sector_benchmark_source"] = stats.get("source")
    for item in results:
        matches = []
        if item.get("asset_type") == "stock":
            if safe_float(item.get("relative_strength_vs_spy"), -999) >= 2 and safe_float(item.get("rs_rank")) >= 70:
                matches.append("Cross-sectional relative strength")
            if safe_float(item.get("intraday_rvol_same_time")) >= 2:
                matches.append("Same-time-of-day relative volume")
            if "liquid" in str(item.get("liquidity_status", "")).lower():
                matches.append("Liquidity and spread")
            if "breakout" in str(item.get("opening_range_status", "")).lower() and "downside" not in str(item.get("opening_range_status", "")).lower():
                matches.append("Opening range and retest")
            if "above session-open" in str(item.get("anchored_vwap_status", "")).lower():
                matches.append("Anchored VWAP")
            if safe_float(item.get("adx")) >= 20 and safe_float(item.get("plus_di")) > safe_float(item.get("minus_di")):
                matches.append("ADX / DMI")
            if "contraction detected" in str(item.get("volatility_contraction_status", "")).lower():
                matches.append("Volatility contraction")
            matches.extend(item.get("early_runner_signal_matches") or [])
            matches.extend(item.get("early_warning_matches") or [])
        elif item.get("asset_type") == "crypto":
            matches.extend(item.get("early_warning_matches") or [])
        item["research_signal_matches"] = matches
    for item in results:
        classify_setup(item, regime["score_adjustment"], regime)
    results.sort(key=lambda item: item["score"], reverse=True)
    crypto_shadow_comparison = collect_crypto_shadow_comparisons(
        crypto_results, os.getenv("GITHUB_RUN_NUMBER"), os.getenv("GITHUB_RUN_ID")
    ) if crypto_results else {"mode": "shadow_only", "sample_limit": CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN,
                               "symbols_sampled": 0, "records_this_scan": 0, "summary": {}, "recent_records": []}
    provider_activity = scan_metrics_snapshot()
    alpaca_status_counts = {}
    for item in stock_results:
        status = item.get("alpaca_quote_status", "not_reported")
        alpaca_status_counts[status] = alpaca_status_counts.get(status, 0) + 1
    webull_comparison_records = [
        {"symbol": item.get("symbol"), "webull_error": item.get("webull_error"), "webull_status": item.get("webull_status"), **(item.get("webull_yahoo_comparison") or {})}
        for item in stock_results if item.get("webull_status") in ("shadow_quote_received", "shadow_quote_unavailable")
    ]
    webull_history = persist_webull_yahoo_history(
        webull_comparison_records, now_iso(), os.getenv("GITHUB_RUN_ID")
    ) if WEBULL_SHADOW_MODE else {}
    return {
        **response_payload(asset, stock_symbols + crypto_symbols, results, errors),
        "market_conditions": regime,
        "stock_market_breadth": stock_market_breadth,
        "stock_universe_strength": stock_universe_strength,
        "stock_candidates": len(stock_results),
        "crypto_candidates": len(crypto_results),
        "diagnostics": {
            "stocks": {
                "symbols_requested": len(stock_symbols),
                "candidates_returned": len(stock_results),
                "scan_errors": len(stock_errors),
                "not_returned_or_insufficient_data": max(0, len(stock_symbols) - len(stock_results) - len(stock_errors)),
                "stages": {stage: sum(1 for x in stock_results if x.get("stage") == stage) for stage in ("BUY", "ACTIONABLE SETUP", "DEVELOPING RUNNER", "DEVELOPING SETUP", "EARLY SETUP", "EARLY WATCH", "LATE RUNNER — CHASE RISK", "LOW-CONVICTION WATCH", "PRICE UNVERIFIED — WATCH ONLY", "DATA QUALITY — WATCH ONLY")},
            },
            "crypto": {
                "symbols_requested": len(crypto_symbols),
                "candidates_returned": len(crypto_results),
                "scan_errors": len(crypto_errors),
                "not_returned_or_insufficient_data": max(0, len(crypto_symbols) - len(crypto_results) - len(crypto_errors)),
                    "stages": {stage: sum(1 for x in crypto_results if x.get("stage") == stage) for stage in ("BUY", "ACTIONABLE SETUP", "DEVELOPING SETUP", "EARLY WATCH", "LOW-CONVICTION WATCH", "PRICE UNVERIFIED — WATCH ONLY", "DATA QUALITY — WATCH ONLY")},
                "price_validation": {
                    "verified": sum(1 for x in crypto_results if x.get("price_validation_passed")),
                    "unverified": sum(1 for x in crypto_results if not x.get("price_validation_passed")),
                    "methods": {method: sum(1 for x in crypto_results if x.get("price_validation_method") == method) for method in ("kraken_latest_trade_cross_check", "coinbase_ticker_timestamp", "kraken_coinbase_cross_check")},
                },
            },
            "alpaca_quote_status": {
                "credentials_configured": bool(os.getenv("ALPACA_API_KEY", "").strip() and os.getenv("ALPACA_API_SECRET", "").strip()),
                "feed": os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower() or "iex",
                "candidate_status_counts": alpaca_status_counts,
                "provider_activity": provider_activity.get("providers", {}).get("alpaca_snapshot_batch", {"calls": 0, "failures": 0, "total_seconds": 0.0, "max_seconds": 0.0, "error_counts": {}}),
            },
            "provider_note": "Counts show requested symbols and scan outcomes. A symbol not returned may have insufficient history, invalid market data, or a provider error; low scores alone no longer remove otherwise valid discoveries.",
            "crypto_shadow_comparison": crypto_shadow_comparison,
            "webull_quote_status": "shadow_mode" if WEBULL_SHADOW_MODE else ("enabled" if WEBULL_QUOTE_ENABLED else "disabled_pending_authorization"),
            "webull_quote_note": (
                "Shadow mode only: read-only Webull quotes are sampled and compared with Yahoo; they do not affect price selection, verification, scoring, BUY gates, or paper trades."
                if WEBULL_SHADOW_MODE else "Webull quote lookups remain opt-in; Yahoo/Alpaca scanning continues independently."
            ),
            "webull_yahoo_comparison": webull_history,
            "webull_shadow_sample": {
                "enabled": WEBULL_SHADOW_MODE,
                "credentials_configured": webull_credentials_configured,
                "sample_limit_per_scan": WEBULL_SHADOW_MAX_SYMBOLS_PER_SCAN,
                "symbols_selected": len(WEBULL_SHADOW_SYMBOLS),
            },
            "massive_live_quote_status": "enabled" if MASSIVE_LIVE_QUOTE_ENABLED else "disabled_pending_entitlement_check",
            "massive_live_quote_note": "Optional last-trade cross-check is disabled by default. Enable MASSIVE_LIVE_QUOTE_ENABLED=true only if the Massive Stocks plan includes /v2/last/trade; 15-minute delayed data is accepted only when its timestamp is within 45 minutes.",
            "market_session": market_session_status(),
            "scan_duration_seconds": round(time.monotonic() - scan_started, 2),
            "timing_breakdown": {
                "universe_discovery_seconds": round(discovery_seconds, 2),
                "asset_scans_wall_seconds": round(asset_scans_seconds, 2),
                "market_conditions_seconds": round(market_conditions_seconds, 2),
                "total_seconds": round(time.monotonic() - scan_started, 2),
                "provider_activity": provider_activity,
                "http_rate_limit_events": provider_activity.get("provider_http_429_count", 0),
                "http_server_error_events": provider_activity.get("provider_http_5xx_count", 0),
            },
            "massive_fallback_policy": {
                "max_requests_per_scan": MASSIVE_MAX_REQUESTS_PER_SCAN,
                "spacing_seconds": 12,
                "note": "Massive fallback is capped to three requests per scan; remaining Yahoo failures use Stooq to avoid serial rate-limit waits.",
            },
        },
    }


@app.get("/intelligence", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
@app.get("/research", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def research_page():
    html = (TEMPLATE_DIR / "intelligence.html").read_text(encoding="utf-8")
    return apply_workspace_preferences(HTMLResponse(html))

@app.get("/intelligence/data", dependencies=[Depends(require_site_login)])
@app.get("/research/data", dependencies=[Depends(require_site_login)])
def research_data():
    durable=latest_history_state()
    if durable:
        paper=durable["paper_tracker"]; lifetime=durable["lifetime"]; buys=durable["buy_signals"]
        synced=durable.get("synced_at"); completed=durable.get("last_scan_completed_at")
        run_id=durable.get("scan_run_id"); source=durable.get("history_source", "github")
    else:
        def read_state(path):
            try: return json.loads(path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError): return {}
        snapshot=read_state(HISTORY_SNAPSHOT)
        if isinstance(snapshot.get("paper_tracker"),dict) and isinstance(snapshot.get("lifetime"),dict):
            paper=snapshot["paper_tracker"]; lifetime=snapshot["lifetime"]; buys=snapshot.get("buy_signals",[])
            synced=snapshot.get("synced_at") or now_iso(); completed=snapshot.get("last_scan_completed_at")
            run_id=snapshot.get("scan_run_id"); source="temporary_render_snapshot"
        else:
            paper=read_state(PAPER_STATE); lifetime=read_state(PAPER_LIFETIME_STATE)
            buys=read_state(BUY_SIGNAL_STATE).get("signals",[])
            meta=read_state(APP_DIR/"scan_metadata.json"); synced=None
            completed=meta.get("last_scan_completed_at"); run_id=meta.get("run_id")
            source="temporary_render_fallback"
    records=list(paper.values()) if isinstance(paper,dict) else []
    # Exclude stock outcomes sampled from EOD, historical, stale, or unknown price data.
    outcome_records=[r for r in records if has_session_aware_early_runner_baseline(r)]
    fields=("relative_strength_vs_spy","relative_strength_vs_iwm","rs_rank","intraday_rvol_same_time",
            "same_time_relative_volume","anchored_vwap_status","anchored_vwap","opening_range_status","adx","plus_di","minus_di","adx_dmi_status",
            "spread_pct","dollar_volume","liquidity_status","volatility_contraction_status","atr_contraction_pct","base_tightness_pct",
            "premarket_gap_pct","premarket_volume","premarket_rvol_same_time","premarket_high","premarket_low",
            "cumulative_rvol_same_time","volume_acceleration_3bar","float_rotation_pct","float_shares","shares_outstanding",
            "short_percent_float","higher_highs_higher_lows","pullback_from_session_high_pct","premarket_high_reclaimed",
            "catalyst_labels","catalyst_dilution_status","dilution_flags","catalyst_headline_count",
            "early_runner_research_score","early_runner_score_components",
            "buy_intelligence","legacy_score","score_model","score_model_note",
            "breakout_proximity_pct","breakout_reference_price","breakout_proximity_status",
            "volatility_compression_percentile","liquidity_sweep_detected","liquidity_sweep_status",
            "accumulation_signal_score","accumulation_signal_status","multi_timeframe_alignment",
            "multi_timeframe_status","early_warning_matches",
            "weekly_confirmation","weekly_confirmation_status","weekly_breakout_confluence",
            "breakout_level_class","breakout_level_reference_price","breakout_level_lookback_sessions",
            "universe_rs_rank","universe_rs_percentile","relative_strength_vs_universe_median_pct",
            "universe_rs_sample_size","universe_rs_rank_change_5d","market_breadth",
            "crqs","crqs_tier","crqs_components","crqs_data_coverage","crqs_entry_score","crqs_entry_penalty","crqs_features",
             "data_quality","daily_data_quality","intraday_data_quality","hourly_data_quality")
    # Cohort outcomes use only verified candidate records with a completed 5-day observation.
    # A runner is defined conservatively as a +20% 5-day observed return; this is a research label, not a forecast.
    cohort_rules = {
        "Float rotation >=20%": lambda f: safe_float(f.get("float_rotation_pct"), 0) >= 20,
        "Volume acceleration >=1.8x": lambda f: safe_float(f.get("volume_acceleration_3bar"), 0) >= 1.8,
        "Cumulative RVOL >=1.5x": lambda f: safe_float(f.get("cumulative_rvol_same_time"), 0) >= 1.5,
        "Premarket RVOL >=1.5x": lambda f: safe_float(f.get("premarket_rvol_same_time"), 0) >= 1.5,
        "Higher highs / higher lows": lambda f: f.get("higher_highs_higher_lows") is True,
        "Classified catalyst": lambda f: bool(f.get("catalyst_labels")),
        "Early runner score >=50": lambda f: safe_float(f.get("early_runner_research_score"), 0) >= 50,
    }
    early_runner_cohorts = {}
    for label, predicate in cohort_rules.items():
        sample = [r for r in outcome_records if isinstance(r, dict)
                  and isinstance((r.get("early_runner_outcomes") or {}).get("5d"), dict)
                  and predicate(r.get("early_runner_initial_feature_snapshot") or {})]
        returns = [safe_float((r.get("early_runner_outcomes") or {}).get("5d", {}).get("return_pct"), 0) for r in sample]
        runners = sum(1 for value in returns if value >= 20)
        early_runner_cohorts[label] = {
            "observations": len(sample), "runner_count": runners,
            "runner_rate_pct": round(runners / len(sample) * 100, 1) if sample else None,
            "average_5d_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "definition": "5-day sampled return >=20%; only completed, verified observations",
        }
    runner_intelligence = build_runner_intelligence_report(records, buys)
    massive_key=bool(os.getenv("MASSIVE_API_KEY","").strip())
    webull_keys=bool(os.getenv("WEBULL_APP_KEY","").strip() and os.getenv("WEBULL_APP_SECRET","").strip())
    return {"synced_at":synced,"last_scan_completed_at":completed,"scan_run_id":run_id,
            "runner_intelligence":runner_intelligence,
            "history_source":source,"paper_tracker":paper,"lifetime":lifetime,"buy_signals":buys,
            "market_session":market_session_status(),
            "quote_provider_status":{
                "massive_live_quotes":("Massive live quotes: enabled" if MASSIVE_LIVE_QUOTE_ENABLED and massive_key else
                    "Massive live quotes: disabled by safety setting" if not MASSIVE_LIVE_QUOTE_ENABLED else "Massive live quotes: API key missing"),
                "webull":("Webull: enabled" if WEBULL_QUOTE_ENABLED and webull_keys else
                    "Webull: authorization pending / provider disabled" if webull_keys else "Webull: credentials not configured")},
            "research_field_coverage":{key:sum(1 for r in records if isinstance(r,dict) and r.get(key) is not None) for key in fields},
            "early_runner_cohorts":early_runner_cohorts,
            "research_outcomes_summary":{
                "tracked":sum(1 for r in outcome_records if r.get("research_entry_price")),
                "mfe_average_pct":round(sum(safe_float(r.get("research_max_favorable_pct")) for r in outcome_records if r.get("research_max_favorable_pct") is not None)/max(1,sum(1 for r in outcome_records if r.get("research_max_favorable_pct") is not None)),2),
                "mae_average_pct":round(sum(safe_float(r.get("research_max_adverse_pct")) for r in outcome_records if r.get("research_max_adverse_pct") is not None)/max(1,sum(1 for r in outcome_records if r.get("research_max_adverse_pct") is not None)),2),
                "breakout_events":sum(1 for r in outcome_records if r.get("research_breakout_at")),
                "false_breakouts":sum(1 for r in outcome_records if r.get("research_breakout_status")=="failed"),
                "forward_returns":{h:{"observations":sum(1 for r in outcome_records if h in (r.get("research_outcomes") or {})),
                    "average_return_pct":round(sum(safe_float((r.get("research_outcomes") or {}).get(h,{}).get("return_pct")) for r in outcome_records if h in (r.get("research_outcomes") or {}))/max(1,sum(1 for r in outcome_records if h in (r.get("research_outcomes") or {}))),2)} for h in ("1h","4h","24h","72h","5d","10d")}
            }}
