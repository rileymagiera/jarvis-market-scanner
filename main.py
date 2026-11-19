import os
import json
import hmac
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
from io import StringIO
import yfinance as yf
from early_runner import compute_early_runner_metrics, compute_early_warning_metrics, compute_stock_market_breadth, rank_stock_universe_strength, summarize_crypto_cross_section, set_crypto_relative_strength_coverage, build_data_quality_record, apply_entry_quality_gate, has_session_aware_early_runner_baseline
from buy_intelligence import build_buy_intelligence, blend_buy_score
from paper_trading import load_account as load_paper_account, account_summary as summarize_paper_account
from fastapi import FastAPI, Query, Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, Response
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
    """Schedule-based US equity session label; holidays are not independently verified."""
    eastern = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    minute = eastern.hour * 60 + eastern.minute
    weekday = eastern.weekday() < 5
    if not weekday:
        phase = "CLOSED — WEEKEND"
    elif 4 * 60 <= minute < 9 * 60 + 30:
        phase = "PRE-MARKET"
    elif 9 * 60 + 30 <= minute < 16 * 60:
        phase = "REGULAR SESSION"
    elif 16 * 60 <= minute < 20 * 60:
        phase = "AFTER-HOURS"
    else:
        phase = "CLOSED — OVERNIGHT"
    return {
        "phase": phase,
        "is_regular_session": bool(weekday and 9 * 60 + 30 <= minute < 16 * 60),
        "is_extended_session": bool(weekday and (4 * 60 <= minute < 9 * 60 + 30 or 16 * 60 <= minute < 20 * 60)),
        "as_of": eastern.isoformat(),
        "calendar_note": "Schedule-based Eastern Time estimate; exchange holidays and early closes are not checked.",
    }

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
KRAKEN_TICKER_CACHE = None
KRAKEN_TICKER_CACHE_AT = 0.0
KRAKEN_REQUEST_LOCK = threading.Lock()
KRAKEN_LAST_REQUEST_AT = 0.0
KRAKEN_MIN_REQUEST_INTERVAL_SECONDS = 1.05


def kraken_get(url: str, **kwargs):
    """Serialize public Kraken REST calls and pace starts to stay near 1 req/sec."""
    global KRAKEN_LAST_REQUEST_AT
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
    global KRAKEN_PAIR_MAP
    if KRAKEN_PAIR_MAP is not None:
        return KRAKEN_PAIR_MAP
    response = kraken_get(f"{KRAKEN}/AssetPairs", timeout=15)
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

    unique_symbols = list(dict.fromkeys(str(x).upper() for x in symbols if str(x).strip()))
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


SEC_HEADERS = {
    "User-Agent": "JarvisMarketScanner/2.0 (rileymagiera51@gmail.com)",
    "Accept-Encoding": "gzip, deflate",
}
SEC_TICKER_MAP = None


def sec_recent_filings(symbol: str, days: int = 14) -> list[dict]:
    """Fetch recent material SEC filing metadata; filing contents remain for user review."""
    global SEC_TICKER_MAP
    started = time.monotonic()
    outcome = "failure"
    try:
        if SEC_TICKER_MAP is None:
            mapping_response = HTTP.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=5)
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
        response = HTTP.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS,
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


def webull_latest_bar(symbol: str) -> dict:
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
        response = data_client.market_data.get_history_bar(symbol, "US_STOCK", "M1")
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
                    stamp_value = float(time_value)
                    if stamp_value > 1e12:
                        stamp = datetime.fromtimestamp(stamp_value / 1000, timezone.utc)
                    elif stamp_value > 1e9:
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
    if age_minutes < -5 or age_minutes > 10:
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
    webull_enabled = has_webull_credentials and WEBULL_QUOTE_ENABLED

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

    if webull_enabled:
        try:            # Optional read-only Webull cross-check; explicitly enabled only after
            # app authorization is confirmed. The scan remains usable without it.
            webull_quote = webull_latest_bar(symbol)
            webull_status = "fresh_quote_received"
        except Exception:
            # Missing entitlement, credentials, SDK, or endpoint must not stop a scan.
            webull_status = "unavailable_or_not_entitled"
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

    if webull_quote:
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
    stamp = pd.Timestamp(ticker.get("time")) if ticker.get("time") else None
    if stamp is not None and pd.notna(stamp):
        if stamp.tzinfo is None: stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("UTC")
        as_of = stamp.isoformat()
        age_seconds = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds()
    else:
        as_of, age_seconds = None, None
    passed = bool(as_of and age_seconds is not None and -30 <= age_seconds <= 300 and price > 0)
    normalized = {
        "lastPrice": price,
        "priceChangePercent": (price / opening - 1) * 100,
        "quoteVolume": volume * price,
        "price_as_of": as_of,
        "price_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "price_validation_passed": passed,
        "price_validation_note": "Fresh Coinbase ticker timestamp" if passed else "Coinbase ticker timestamp missing or older than 5 minutes",
        "price_validation_method": "coinbase_ticker_timestamp",
    }
    return normalized, bars, "Coinbase Exchange public API"


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
        "price_source": provider,
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
    return {
        "name": "Jarvis Market Scanner",
        "status": "online",
        "version": "2.0.0",
        "features": ["stock momentum", "crypto momentum", "relative volume", "breakout detection", "news headlines", "illustrative risk levels"],
        "endpoints": ["/health", "/scan", "/scan/premarket", "/scan/stocks", "/scan/crypto", "/symbols"],
    }



APP_DIR = Path(__file__).resolve().parent
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


@app.get("/history", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def history_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis History</title><style>
:root{color-scheme:dark;--bg:#0b1119;--surface:#121e2b;--surface2:#172638;--line:#293b50;--muted:#9aabbd;--text:#edf3fa;--accent:#83b8ff;--green:#63d9a0;--red:#ff9292}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1a2b40 0%,var(--bg) 58%);color:var(--text);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:min(100% - 32px,1320px);margin:0 auto;padding:30px 0 56px}
h1{font-size:clamp(26px,4vw,36px);letter-spacing:-.035em;line-height:1.15;margin:0 0 8px}
h2{font-size:19px;letter-spacing:-.02em;margin:0 0 12px}
h3{font-size:14px;margin:0 0 12px;color:#dce8f5}
.muted,.tiny{color:var(--muted)}.tiny{font-size:12px;line-height:1.5}
#updated{display:inline-flex;align-items:center;gap:7px;margin:5px 0 10px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:#101b28;font-size:12px}
#updated:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 10px #63d9a077}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:20px 0 28px}
.card,.panel{background:linear-gradient(145deg,var(--surface2),var(--surface));border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 8px 24px #0002;min-width:0}
.card .muted{font-size:12px;letter-spacing:.02em}.value{font-size:clamp(21px,3vw,27px);font-weight:750;letter-spacing:-.035em;margin-top:5px;overflow-wrap:anywhere}
.section{margin-top:30px;padding-top:22px;border-top:1px solid #26384b}
.section> .muted{font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px}
table{border-collapse:separate;border-spacing:0;width:100%;min-width:720px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #26384a;font-size:12px;vertical-align:top}
th{position:sticky;top:0;background:#172638;color:#b9c9da;font-size:11px;text-transform:uppercase;letter-spacing:.055em;white-space:nowrap;z-index:1}
tbody tr:nth-child(even){background:#ffffff06}tbody tr:hover{background:#83b8ff0d}
.scroll{overflow:auto;margin-top:12px;border:1px solid var(--line);border-radius:12px;background:#0f1924;overscroll-behavior-x:contain}
.scroll table{margin:0}.scroll td .tiny{margin-top:3px}
.asset-jump{display:block;width:100%;text-align:left;color:inherit;font:inherit;cursor:pointer;transition:border-color .15s,transform .15s}.asset-jump:hover{border-color:var(--accent);transform:translateY(-2px)}.asset-jump:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.pos{color:var(--green);font-weight:650}.neg{color:var(--red)}
.toolbar{display:flex;flex-wrap:wrap;gap:9px;margin:14px 0}
input,select{background:#0d1722;color:var(--text);border:1px solid #40536a;border-radius:9px;padding:10px 12px;font:inherit;min-height:42px}
input{flex:1;min-width:min(100%,190px)}input::placeholder{color:#8193a7}
input:focus,select:focus{outline:2px solid #83b8ff66;border-color:var(--accent)}
.pill{display:inline-block;border:1px solid #40536a;border-radius:999px;padding:3px 9px;font-size:10px;line-height:1.5;white-space:nowrap;background:#1b2b3c}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.empty{text-align:center;padding:20px;color:var(--muted)}

.eyebrow{font-size:10px;letter-spacing:.18em;color:#8fa8c3;font-weight:800;margin-bottom:7px}
.command-header{position:relative;isolation:isolate;overflow:hidden;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 20px;border:1px solid #315b70;border-radius:20px;background:linear-gradient(112deg,#0e1c2a 0%,#142d40 52%,#101b29 100%);box-shadow:0 16px 42px #0005,inset 0 1px #a9e9ff12}
.command-header:before{content:"";position:absolute;z-index:-1;inset:0;background:radial-gradient(ellipse at 15% 50%,#32718b35,transparent 42%),radial-gradient(ellipse at 88% 10%,#53d7c51c,transparent 34%);pointer-events:none}
.command-header:after{content:"";position:absolute;z-index:-1;right:27%;top:0;width:1px;height:100%;background:linear-gradient(transparent,#76c9d533,transparent);pointer-events:none}
.brand-lockup{display:flex;align-items:center;gap:16px;min-width:0}.brand-mark{position:relative;width:56px;height:56px;flex:0 0 56px;border:1px solid #6babc2;border-radius:17px;display:grid;place-items:center;background:linear-gradient(145deg,#2b5876,#102236 72%);font-size:30px;font-weight:950;color:#e4f8ff;box-shadow:0 0 0 4px #7bd8e00b,0 0 30px #5cbcff35,inset 0 1px #ffffff30}.brand-mark:after{content:"";position:absolute;top:7px;right:7px;width:7px;height:7px;border-radius:50%;background:#8fffe0;box-shadow:0 0 12px 3px #5ce7d477}
.command-header h1{font-size:clamp(29px,4.4vw,40px);font-weight:900;letter-spacing:.14em;margin:0;line-height:1;text-shadow:0 2px 18px #6bc7df20}.title-dot{color:#77e1d5}.command-header .eyebrow{font-size:9px;letter-spacing:.23em;color:#83d9d4;font-weight:850;margin-bottom:8px}.brand-sub{font-size:9px;letter-spacing:.15em;color:#a5bbce;margin-top:8px}
.header-right{text-align:right;position:relative;z-index:1}.live-status{font-size:11px;font-weight:700;color:#d4e7f3;border:1px solid #42677b;background:#0c1a27c9;padding:9px 13px;border-radius:999px;display:inline-flex;align-items:center;max-width:100%;white-space:normal;box-shadow:inset 0 1px #ffffff0b,0 5px 18px #0002}
.live-status:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:8px;box-shadow:0 0 10px #63d9a077;flex:0 0 auto}
.header-right .tiny{font-size:9px;letter-spacing:.13em;margin-top:9px;color:#91a9bd}
.hero-panel{position:relative;overflow:hidden;display:grid;grid-template-columns:1.2fr .8fr;min-height:245px;margin:22px 0 28px;padding:28px 34px;border:1px solid #294d65;border-radius:20px;background:radial-gradient(ellipse at 78% 48%,#17445a 0%,#14283a 36%,#101b29 78%);box-shadow:0 18px 50px #0004}
.hero-copy{position:relative;z-index:2;align-self:center;max-width:520px}.hero-copy h2{font-size:clamp(29px,4vw,45px);line-height:1.04;letter-spacing:-.045em;margin:5px 0 12px}.hero-copy h2 span{color:#78d9d0}.hero-copy p{font-size:13px;color:#b4c7d8;max-width:430px;margin:0;line-height:1.65}
.radar-art{position:relative;align-self:center;justify-self:center;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,#183b50 0%,#12283a 48%,#0d1b28 70%);border:1px solid #4e8498;box-shadow:0 0 55px #43b6bd22,inset 0 0 35px #56c9cf18}
.radar-ring{position:absolute;border:1px solid #4f9ca277;border-radius:50%;inset:15%}.ring-two{inset:30%}.ring-three{inset:45%}.radar-art:before,.radar-art:after{content:"";position:absolute;background:#4f9ca255}.radar-art:before{height:1px;left:7%;right:7%;top:50%}.radar-art:after{width:1px;top:7%;bottom:7%;left:50%}
.radar-sweep{position:absolute;inset:9%;border-radius:50%;background:conic-gradient(from 5deg,transparent 0deg,transparent 290deg,#5ed8d044 345deg,#5ed8d088 360deg);clip-path:circle(50%);transform:rotate(20deg)}
.radar-core{position:absolute;left:48%;top:48%;width:9px;height:9px;border-radius:50%;background:#8fffe0;box-shadow:0 0 18px 6px #5ce7d477}
.radar-label{position:absolute;font-size:8px;letter-spacing:.12em;color:#a8d6df;background:#102637;border:1px solid #356175;padding:4px 6px;border-radius:5px}.radar-label-one{top:15%;right:-8%}.radar-label-two{bottom:16%;left:-10%}.radar-label-three{bottom:3%;right:15%}
.radar-section{margin-top:0;padding-top:2px}.section-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:14px}.section-heading h2{font-size:22px;margin:0 0 4px}
.radar-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:1px;border:1px solid #293d50;border-radius:12px;overflow:hidden;background:#293d50;margin-bottom:14px}
.radar-stat{padding:12px 15px;background:#111e2b;min-width:0}.radar-stat strong{display:block;font-size:23px;line-height:1.1;font-weight:800;font-variant-numeric:tabular-nums}.radar-stat span{display:block;color:#8fa7bb;font-size:9px;letter-spacing:.1em;margin-top:6px}
.opportunity-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
.buy-floor{margin:0 0 26px;padding:18px;border:1px solid #356d68;border-radius:16px;background:linear-gradient(135deg,#112b2b,#101d29 72%)}.buy-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.buy-card{padding:14px;border:1px solid #387b69;border-radius:12px;background:#102a27;min-width:0}.buy-card-top{display:flex;justify-content:space-between;gap:10px;align-items:center}.buy-tag{font-size:10px;font-weight:850;letter-spacing:.12em;color:#9cf0c8;border:1px solid #438b70;border-radius:99px;padding:4px 8px}.buy-symbol{font-size:20px;font-weight:850}.buy-detail{font-size:11px;color:#c2d6d2;line-height:1.5;margin-top:8px}.buy-return{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums}.buy-meta{font-size:10px;color:#93b2ac;margin-top:5px}.op-card{background:linear-gradient(145deg,#172b3b,#101c29);border:1px solid #2c4659;border-radius:14px;padding:15px;min-width:0;transition:border-color .15s,transform .15s}.op-card:hover{border-color:#5c9da6;transform:translateY(-2px)}
.op-card.featured{grid-column:span 2;display:grid;grid-template-columns:1.1fr 1fr;column-gap:20px;border-color:#3e8d8e;background:linear-gradient(115deg,#18384a,#132637 65%,#10212e);box-shadow:inset 3px 0 #66d8cc,0 10px 28px #0003}
.op-card.featured .op-card-top{grid-column:1/-1}.op-card.featured .op-symbol{font-size:27px}.op-card.featured .op-reason{align-self:center;font-size:13px}.op-card.featured .op-metrics{align-self:center}
.op-card-top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}.op-symbol{font-size:19px;font-weight:850;letter-spacing:-.02em}.op-kind{font-size:9px;color:#9db1c3;text-transform:uppercase;letter-spacing:.13em}
.op-stage{display:inline-flex;border:1px solid #48617b;background:#20364b;color:#d5e8fa;border-radius:999px;padding:5px 9px;font-size:9px;line-height:1.3;letter-spacing:.03em}
.op-stage.action{border-color:#397e69;background:#16392f;color:#9ae5c3}.op-stage.late{border-color:#875e42;background:#392719;color:#f1bd91}
.op-metrics{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0}.op-metric strong{display:block;font-size:17px;font-variant-numeric:tabular-nums}.op-metric span{font-size:9px;color:#91a7ba;letter-spacing:.04em}
.op-reason{font-size:11px;color:#c4d2e0;line-height:1.5;margin-top:8px}
.op-quick-quality{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:8px 0;padding:9px 11px;border:1px solid #34566a;border-radius:10px;background:#102536}.op-quick-quality>span{font-size:9px;letter-spacing:.08em;color:#91b2c4}.op-quick-quality strong{font-size:16px;color:#e6f5fb;font-variant-numeric:tabular-nums}.op-quick-quality em{font-style:normal;font-size:9px;color:#9be2d1;margin-left:auto}.op-details{margin-top:9px;border:1px solid #30495b;border-radius:10px;background:#0d1c29;overflow:hidden}.op-details>summary{cursor:pointer;list-style:none;padding:10px 11px;color:#9ed5d8;font-size:10px;font-weight:750;letter-spacing:.04em}.op-details>summary::-webkit-details-marker{display:none}.op-details>summary:after{content:'＋';float:right;color:#8caec0}.op-details[open]>summary:after{content:'−'}.op-details-body{padding:0 10px 10px}.op-details-body .op-front-quality{margin:8px 0}.op-details-body .tiny{line-height:1.45;margin:4px 0}.op-risk{font-size:10px;color:#ffb0a6;margin-top:8px}.op-front-quality{border:1px solid #34566a;border-radius:11px;padding:10px 11px;margin:10px 0;background:linear-gradient(135deg,#102a38,#102131)}.op-front-quality-top{display:flex;justify-content:space-between;align-items:center;gap:8px}.op-front-quality-top>div>span,.op-front-plan span{display:block;font-size:9px;letter-spacing:.08em;color:#91b2c4}.op-front-quality-top strong{display:block;font-size:25px;line-height:1.1;color:#e6f5fb;font-variant-numeric:tabular-nums}.op-front-quality-top strong small{font-size:12px;color:#8faabd}.op-quality-label{font-size:9px;font-weight:800;letter-spacing:.08em;color:#9be2d1;border:1px solid #38695f;background:#15352f;border-radius:99px;padding:5px 8px}.op-quality-track{height:5px;background:#263b4b;border-radius:9px;overflow:hidden;margin:8px 0}.op-quality-track i{display:block;height:100%;background:linear-gradient(90deg,#e0a46b,#76d6bd);border-radius:9px}.op-evidence-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 12px}.op-evidence-item>div{display:flex;justify-content:space-between;gap:5px;font-size:8px;color:#9ab2c3;letter-spacing:.04em}.op-evidence-item b{color:#d8e7f0;font-variant-numeric:tabular-nums}.op-evidence-item i{display:block;height:3px;background:#263a49;border-radius:8px;overflow:hidden;margin-top:3px}.op-evidence-item em{display:block;height:100%;background:#65cbbb;border-radius:8px}.op-front-quality.unavailable{color:#e5bd92;background:#30251d;border-color:#705238}.op-front-quality.unavailable>div{font-weight:800}.op-front-quality.unavailable span{font-size:9px;letter-spacing:.08em}.op-front-quality.unavailable strong{display:block;font-size:19px}.op-front-quality.unavailable small{color:#c8ad91;font-size:10px}.op-front-plan{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}.op-front-plan>div{min-width:0;background:#0b1824;border:1px solid #31495d;border-radius:9px;padding:9px}.op-front-plan b{display:block;margin-top:4px;font-size:12px;color:#e4eff8;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.op-front-plan-note{font-size:10px;color:#e7b98e;margin:8px 0}.op-card.featured{display:block}.op-flippable{perspective:1100px;cursor:pointer;outline:none}.op-flippable:focus-visible{outline:2px solid var(--accent);outline-offset:4px}.op-flip-inner{display:grid;transform-style:preserve-3d;transition:transform .55s cubic-bezier(.2,.75,.25,1);min-height:100%;position:relative}.op-flippable.is-flipped .op-flip-inner{transform:rotateY(180deg)}.op-face{grid-area:1/1;backface-visibility:hidden;-webkit-backface-visibility:hidden;min-width:0}.op-face-back{transform:rotateY(180deg);display:flex;flex-direction:column;gap:8px}.op-back-title{font-size:10px;letter-spacing:.12em;color:#8edbd5;font-weight:800}.op-plan-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.op-plan-cell{border:1px solid #31495d;border-radius:9px;padding:8px;background:#0b1824}.op-plan-cell strong{display:block;font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.op-plan-cell span{display:block;color:#91a7ba;font-size:9px;letter-spacing:.04em}.op-confidence{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;border-radius:9px;background:#193447;border:1px solid #3b657a}.op-confidence strong{font-size:18px}.op-confidence small{display:block;color:#a9c0d0;font-size:9px}.op-confidence.high{border-color:#397e69;background:#16392f}.op-confidence.low{border-color:#875e42;background:#392719}.op-back-note{font-size:10px;color:#a9bdce;line-height:1.45}.op-front-explanation{font-size:10px;color:#a9bdce;line-height:1.45;margin:0 0 9px;padding:9px 10px;border:1px solid #31564f;border-radius:9px;background:#102824}.op-front-explanation>div+div{margin-top:6px;color:#93b8aa}.target-math{border:1px solid #35546a;border-radius:12px;padding:11px;background:linear-gradient(145deg,#102334,#0c1926);display:grid;gap:9px}.target-math-head{display:flex;justify-content:space-between;align-items:center;gap:8px}.target-math-head b{font-size:11px;letter-spacing:.09em;color:#d7eaf7}.target-math-head span{font-size:9px;color:#8eb6c9;border:1px solid #34546b;border-radius:99px;padding:4px 7px}.target-math-inputs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.target-math-input{padding:7px 8px;border-radius:8px;background:#0a1722;border:1px solid #263f52;min-width:0}.target-math-input span{display:block;font-size:8px;color:#8faabd;letter-spacing:.06em}.target-math-input strong{display:block;font-size:12px;color:#e3eef7;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin-top:3px}.target-math-risk{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;background:#2b241b;border:1px solid #68513a;border-radius:8px}.target-math-risk span{font-size:9px;color:#d8b994}.target-math-risk strong{font-size:13px;color:#ffdbad;font-variant-numeric:tabular-nums}.target-math-level{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;padding:9px 10px;border-radius:9px;border:1px solid #315c54;background:#102b29}.target-math-level.level-two{border-color:#4b547b;background:#1c2238}.target-math-level small{display:block;color:#9bb9b2;font-size:8px;letter-spacing:.07em}.target-math-level.level-two small{color:#b1b8dc}.target-math-equation{font-size:10px;color:#d6e3ed;line-height:1.5;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin-top:3px}.target-math-result{text-align:right}.target-math-result strong{font-size:16px;color:#a6ebc9;font-variant-numeric:tabular-nums}.target-math-level.level-two .target-math-result strong{color:#c4caff}.target-math-result span{display:block;font-size:8px;color:#a9bdce}.target-math-foot{font-size:9px;color:#9bb0c1;line-height:1.45}.target-math-foot b{color:#d3e1eb}.op-flip-hint{font-size:9px;color:#86c9d1;margin-top:10px;letter-spacing:.04em}.op-empty{border:1px dashed #40546a;border-radius:12px;padding:20px;color:var(--muted);font-size:13px;grid-column:1/-1}.footnote{margin-top:13px}.timeline-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:10px}.timeline-card{border:1px solid #2c4659;border-radius:12px;padding:13px;background:linear-gradient(145deg,#172b3b,#101c29);min-width:0}.timeline-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.timeline-symbol{font-size:15px;font-weight:850}.timeline-stage{font-size:9px;border:1px solid #48617b;border-radius:99px;padding:4px 7px;color:#d5e8fa}.timeline-events{margin-top:9px;border-left:1px solid #426077;padding-left:10px;display:grid;gap:7px}.timeline-event{font-size:10px;color:#b6c8d8;line-height:1.4}.timeline-event b{color:#e4eff8}.settings-panel{display:flex;flex-wrap:wrap;gap:12px;align-items:end;border:1px solid var(--line);border-radius:13px;background:#101b28;padding:14px}.settings-panel label{display:grid;gap:5px;color:#aebfd0;font-size:11px;min-width:170px}.settings-panel label select{width:100%}.settings-panel .setting-check{display:flex;align-items:center;gap:7px;min-width:190px}.settings-panel .setting-check input{flex:0 0 auto;min-width:0;min-height:0;accent-color:#83b8ff}.settings-panel button{border:1px solid #40536a;background:#1b2b3c;color:var(--text);border-radius:9px;padding:10px 12px;min-height:42px;font:inherit;font-size:11px;cursor:pointer}.compact-mode .op-card,.compact-mode .timeline-card,.compact-mode .buy-card{padding:10px}.compact-mode .op-metrics{gap:10px;margin:6px 0}.compact-mode .timeline-events{gap:4px}
.archive{margin-top:28px;border:1px solid var(--line);border-radius:15px;background:#0e1823;overflow:hidden}
.archive>summary{list-style:none;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 18px;font-weight:700}
.archive>summary::-webkit-details-marker{display:none}
.archive>summary:after{content:"＋";color:var(--accent);font-size:18px;margin-left:auto}
.archive[open]>summary:after{content:"−"}
.summary-note{font-size:11px;color:var(--muted);font-weight:400}
.archive-body{padding:0 16px 18px;border-top:1px solid var(--line)}
.archive-title{margin:18px 0 0;font-size:16px}
.archive-body .section:first-of-type{margin-top:22px}
.archive-body .section{margin-top:24px}
@media(max-width:700px){.command-header{align-items:flex-start;flex-direction:column;padding:16px;border-radius:16px}.command-header:after{display:none}.brand-lockup{gap:12px}.brand-mark{width:49px;height:49px;flex-basis:49px;border-radius:14px;font-size:27px}.header-right{text-align:left;width:100%}.live-status{font-size:10px}.hero-panel{grid-template-columns:1fr;min-height:0;padding:24px}.radar-art{width:155px;height:155px;margin:20px auto 0}.radar-label{font-size:7px}.radar-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.opportunity-grid,.buy-grid{grid-template-columns:1fr}.op-card.featured{grid-column:auto;display:block}.op-card.featured .op-symbol{font-size:22px}.section-heading{align-items:flex-start;flex-direction:column}.archive>summary{align-items:flex-start;flex-wrap:wrap}.summary-note{width:100%}.archive-body{padding:0 10px 14px}}

@media(max-width:700px){main{width:calc(100% - 24px);padding:22px 0 40px}.cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.card,.panel{padding:12px}.section{margin-top:24px;padding-top:18px}th,td{padding:10px;font-size:11px}#updated{max-width:100%;white-space:normal}}
@media(max-width:380px){.cards{grid-template-columns:1fr 1fr}.value{font-size:19px}}

/* Action Center hierarchy and filters */
.action-center{margin:0 0 26px;padding:18px;border:1px solid #355f70;border-radius:17px;background:linear-gradient(140deg,#172d3e,#101d2a 72%);box-shadow:0 10px 30px #0003}.action-center-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:12px}.action-center-head h2{margin:0;font-size:22px}.action-center-head .muted{font-size:12px}.action-top-pick{cursor:pointer;transition:border-color .15s,transform .15s}.action-top-pick:hover,.action-top-pick:focus-visible{border-color:#67cfc4;transform:translateY(-1px);outline:none}.action-top-pick{border:1px solid #45657c;border-radius:13px;background:#0e1b28;padding:15px;margin:10px 0 14px;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}.action-pick-title{font-size:11px;color:#9bb6ca;letter-spacing:.12em;font-weight:800}.action-pick-symbol{font-size:23px;font-weight:850;margin:4px 0}.action-pick-reason{font-size:12px;color:#b9cad9}.action-pick-status{font-size:11px;font-weight:800;padding:6px 9px;border-radius:999px;border:1px solid #45657c;background:#1d3347;white-space:nowrap}.action-filter-row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 5px}.action-filter{appearance:none;cursor:pointer;border:1px solid #3b5268;border-radius:999px;padding:8px 12px;background:#101c29;color:#c6d6e5;font:inherit;font-size:11px;font-weight:750;letter-spacing:.03em}.action-filter.active{border-color:#67cfc4;background:#183a3b;color:#c7fff1;box-shadow:0 0 0 1px #67cfc433}.action-filter:focus-visible{outline:2px solid #83b8ff;outline-offset:2px}.action-counts{display:flex;gap:12px;flex-wrap:wrap;color:#95abc0;font-size:11px;margin-top:10px}.action-counts strong{color:#e6f1fa}.op-card[hidden]{display:none!important}@media(max-width:700px){.action-center{padding:13px}.action-top-pick{grid-template-columns:1fr}.action-center-head h2{font-size:19px}}


/* Performance & Momentum Lab */
.analytics-suite{margin:22px 0 14px;padding:17px;border:1px solid #31546a;border-radius:17px;background:linear-gradient(145deg,#132637,#0d1925);box-shadow:0 12px 30px #0002}
.analytics-suite-head,.analytics-panel-head{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;flex-wrap:wrap}
.analytics-suite-head{margin-bottom:13px}.analytics-suite-head h2{font-size:20px;margin:0 0 4px}.analytics-suite-head .muted,.analytics-panel-head .muted{font-size:10px}
.analytics-live-tag,.analytics-count{font-size:9px;letter-spacing:.09em;color:#a9ded5;border:1px solid #35685f;background:#14312e;border-radius:99px;padding:5px 8px;white-space:nowrap}
.analytics-score-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:11px}
.analytics-score{background:#0c1a27;border:1px solid #30495d;border-radius:10px;padding:11px;min-width:0}
.analytics-score strong{display:block;font-size:clamp(18px,2.6vw,24px);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.analytics-score span{display:block;color:#8faabd;font-size:8px;letter-spacing:.1em;margin-top:5px}
.analytics-two-col{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.analytics-panel{border:1px solid #30495d;border-radius:12px;background:#0e1c29;padding:13px;min-width:0}
.analytics-panel-head{margin-bottom:10px}.analytics-panel-head h3{margin:0 0 3px;font-size:14px}.analytics-legend{display:flex;align-items:center;gap:5px;color:#9bb0c1;font-size:9px}
.analytics-legend i{width:8px;height:8px;border-radius:3px;display:inline-block;margin-left:5px}
.heat-up{background:#3eaf83}.heat-flat{background:#70879a}.heat-down{background:#c76e69}
.discovery-list,.momentum-list{display:grid;gap:7px}
.discovery-row,.momentum-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:8px 9px;border:1px solid #263f52;border-radius:9px;background:#112333;min-width:0}
.analytics-asset{font-size:11px;font-weight:850;color:#e2edf5}.analytics-sub{font-size:9px;color:#91aabd;margin-top:3px;overflow-wrap:anywhere}
.analytics-return{font-size:13px;font-weight:850;font-variant-numeric:tabular-nums;text-align:right}
.analytics-meter{height:4px;background:#263c4d;border-radius:9px;overflow:hidden;margin-top:6px}.analytics-meter i{display:block;height:100%;border-radius:9px;background:#65cbbb}
.analytics-heat-panel{margin-top:10px}.discovery-heatmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(94px,1fr));gap:6px}
.heat-tile{border:1px solid #405568;border-radius:9px;min-height:65px;padding:8px;display:flex;flex-direction:column;justify-content:space-between;min-width:0;background:#1a2935}
.heat-tile.positive{background:linear-gradient(145deg,#174b3d,#123329);border-color:#347b63}.heat-tile.negative{background:linear-gradient(145deg,#512c32,#342128);border-color:#87505a}.heat-tile.neutral{background:#1b2b39;border-color:#405568}
.heat-tile strong{font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.heat-tile b{font-size:14px;font-variant-numeric:tabular-nums}.heat-tile small{font-size:8px;color:#b0c0cc}
.analytics-footnote{font-size:9px;color:#8399aa;margin-top:9px}
@media(max-width:700px){.analytics-suite{padding:12px}.analytics-score-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.analytics-two-col{grid-template-columns:1fr}.discovery-heatmap{grid-template-columns:repeat(3,minmax(0,1fr))}}
\n/* Command Center + Early Runner Radar presentation layer */
.command-console{margin:4px 0 18px;padding:17px 18px;border:1px solid #315b70;border-radius:17px;background:linear-gradient(120deg,#132c3d 0%,#101e2b 60%,#132a32 100%);box-shadow:0 12px 32px #0003,inset 0 1px #ffffff0b;position:relative;overflow:hidden}
.command-console:after{content:"";position:absolute;right:-55px;top:-95px;width:230px;height:230px;border:1px solid #5fd6c51c;border-radius:50%;box-shadow:0 0 0 22px #5fd6c509,0 0 0 48px #5fd6c506;pointer-events:none}
.command-console-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:relative;z-index:1}
.command-console-kicker{font-size:10px;letter-spacing:.19em;color:#83d9d4;font-weight:850}
.command-console-sub{font-size:11px;color:#9fb4c7;margin-top:3px}
.command-console-state{font-size:9px;letter-spacing:.1em;color:#b9e9dc;border:1px solid #3b796e;background:#15332f;padding:6px 9px;border-radius:99px}
.command-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:14px;position:relative;z-index:1}
.command-metric{padding:11px 12px;border:1px solid #304c60;border-radius:11px;background:#0c1b28c9;min-width:0}
.command-metric strong{display:block;font-size:clamp(20px,3vw,27px);line-height:1.1;font-weight:850;letter-spacing:-.04em;font-variant-numeric:tabular-nums}
.command-metric span{display:block;font-size:8px;letter-spacing:.12em;color:#91adbf;margin-top:6px}
.command-console-foot{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:11px;font-size:10px;color:#9bb3c5;position:relative;z-index:1}
.runner-radar{margin:22px 0 8px;padding:16px;border:1px solid #2e4b5e;border-radius:16px;background:linear-gradient(145deg,#132535,#0e1a26)}
.runner-radar-head{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.runner-radar-head h3{font-size:16px;margin:0 0 3px;color:#e6f2fa}
.runner-radar-head .muted{font-size:11px}
.runner-lanes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.runner-lane{border:1px solid #30495b;border-radius:11px;padding:11px;background:#0d1b27;min-width:0}
.runner-lane-top{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:8px}
.runner-lane-title{font-size:9px;letter-spacing:.1em;font-weight:850;color:#a9c4d5}
.runner-lane-count{font-size:11px;font-weight:850;color:#dff8f0;background:#17352f;border:1px solid #356b60;border-radius:99px;padding:3px 7px}
.runner-lane p{font-size:9px;line-height:1.4;color:#8098aa;margin:0 0 8px;min-height:25px}
.runner-chip-list{display:flex;flex-wrap:wrap;gap:5px}
.runner-chip{appearance:none;border:1px solid #38566b;background:#172b3c;color:#dcebf5;border-radius:7px;padding:5px 7px;font:700 10px system-ui;cursor:pointer}
.runner-chip:hover,.runner-chip:focus-visible{border-color:#70d8ca;background:#1a3a40;outline:none}
.runner-empty{font-size:10px;color:#73899a;padding:5px 0}
@media(max-width:700px){.command-console{padding:13px}.command-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.runner-lanes{grid-template-columns:repeat(2,minmax(0,1fr))}.runner-radar{padding:12px}}
@media(max-width:390px){.runner-lanes{grid-template-columns:1fr 1fr}.runner-lane{padding:9px}.runner-lane p{min-height:0}}
.top-tabs{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:18px 0 22px;max-width:100%}.top-tabs a{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:8px 12px;border:1px solid #293b50;border-radius:10px;background:#101c29;color:#cbd9e7;text-decoration:none;font-size:12px;font-weight:700;white-space:normal;line-height:1.25;text-align:center}.top-tabs a:hover{border-color:#52728f;background:#17283a} .top-tabs a[aria-current="page"]{border-color:var(--accent);background:#203b55;color:#fff;box-shadow:inset 0 0 0 1px #47617d}@media(max-width:700px){.top-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin:14px 0 18px}.top-tabs a{min-width:0;padding:9px 7px;font-size:11px;overflow-wrap:anywhere}}</style></head><body><main><header class="command-header"><div class="brand-lockup"><div class="brand-mark">J</div><div><div class="eyebrow">MARKET INTELLIGENCE SYSTEM</div><h1>JARVIS<span class="title-dot">.</span></h1><div class="brand-sub">EARLY-MOVE DETECTION · STOCKS + DIGITAL ASSETS</div></div></div><div class="header-right"><div class="live-status" id="updated">Connecting to saved history…</div><div class="tiny">PAPER ONLY · NO LIVE ORDERS</div></div></header><nav class="top-tabs" aria-label="Jarvis dashboard navigation"><a href="/history" aria-current="page">Overview</a><a href="/intelligence">Intelligence</a><a href="/paper-trading">Paper Trading</a><a href="/discovery-performance">Discovery Performance</a><a href="/momentum-evidence">Momentum Evidence</a><a href="/market-breadth">Market Breadth</a><a href="/customize">Customize</a></nav><section class="command-console" aria-label="Jarvis command center"><div class="command-console-head"><div><div class="command-console-kicker">JARVIS · COMMAND CENTER</div><div class="command-console-sub">A live-at-a-glance view of the saved discovery ledger and current radar.</div></div><span class="command-console-state">PAPER SIMULATION · NO LIVE ORDERS</span></div><div class="command-metrics" id="command-metrics"><div class="command-metric"><strong>—</strong><span>TRACKED ASSETS</span></div><div class="command-metric"><strong>—</strong><span>ON RADAR NOW</span></div><div class="command-metric"><strong>—</strong><span>EARLY / BUILDING</span></div><div class="command-metric"><strong>—</strong><span>BUY + ACTIONABLE</span></div></div><div class="command-console-foot"><span id="command-scan-status">Waiting for saved scan data…</span><span id="command-data-status">Price validation pending</span></div></section><div class="action-filter-row" aria-label="Filter radar by asset type"><button type="button" class="action-filter active" data-jasset="all">ALL ASSETS</button><button type="button" class="action-filter" data-jasset="stock">STOCKS</button><button type="button" class="action-filter" data-jasset="crypto">CRYPTO</button></div><div class="action-filter-row" aria-label="Filter radar by setup stage"><button type="button" class="action-filter active" data-jstage="all">ALL STAGES</button><button type="button" class="action-filter" data-jstage="buy">BUY SIGNALS</button><button type="button" class="action-filter" data-jstage="setup">SETUPS</button><button type="button" class="action-filter" data-jstage="early">EARLY RUNNERS</button><button type="button" class="action-filter" data-jstage="watch">WATCH</button></div><div class="action-counts" id="action-counts">Waiting for current candidates</div><section class="action-center" id="action-center"><div class="action-center-head"><div><div class="eyebrow">YOUR DECISION SCREEN</div><h2>Action Center</h2><div class="muted">Start here. Ranked radar candidates below; details stay expandable.</div></div><div class="tiny">YOU APPROVE ALL REAL TRADES · PAPER MODE IS AUTOMATIC</div></div><div class="action-top-pick" id="action-top-pick" role="button" tabindex="0" aria-label="Open full analysis for the top radar candidate"><div><div class="action-pick-title">TOP RADAR CANDIDATE</div><div class="action-pick-symbol">Waiting for scan data…</div><div class="action-pick-reason">Candidates are ranked using the existing scanner.</div></div><span class="action-pick-status">SCAN PENDING</span></div></section><details class="archive" id="candidate-analysis-library"><summary><span>Full candidate analysis</span><span class="summary-note">Detailed cards · price validation · scoring · risk and trade-plan logic</span></summary><div class="archive-body"><section class="radar-section"><div class="section-heading"><div><div class="eyebrow">DEEP-DIVE LIBRARY</div><h2>Candidate analysis</h2><div class="muted">Open a candidate for full evidence, price history, risk flags and any saved trade plan.</div></div><div class="tiny">PAPER SIMULATION · NO LIVE ORDERS</div></div><div id="opportunities" class="opportunity-grid"><div class="empty">Loading candidates…</div></div></section></div></details><section class="runner-radar" id="runner-radar"><div class="runner-radar-head"><div><div class="eyebrow">EARLY-MOVE DETECTION</div><h3>Early Runner Radar</h3><div class="muted">Follow candidates as evidence develops—from early watch to actionable setup. A stage is not a trade instruction.</div></div><div class="tiny">CLICK A SYMBOL TO OPEN ITS ANALYSIS</div></div><div class="runner-lanes" id="runner-radar-lanes"><div class="runner-lane"><div class="runner-empty">Waiting for candidate data…</div></div></div></section>
<details class="archive" id="early-runner-board"><summary><span>Premarket & intraday research detail</span><span class="summary-note">Additional stock runner metrics</span></summary><div class="archive-body"><div class="section-heading"><div><div class="eyebrow">PREMARKET + INTRADAY DISCOVERY</div><h2>Early Runner board</h2><div class="muted">Ranked from the latest completed hourly scan using gap, premarket volume / relative volume, research score, catalysts and dilution flags.</div></div><div class="tiny">DISPLAY ONLY · NO EXTRA SCAN</div></div><div class="cards" id="early-runner-stats"><div class="card"><div class="muted">QUALIFIED RUNNERS</div><div class="value">—</div></div><div class="card"><div class="muted">PRICE VERIFIED</div><div class="value">—</div></div><div class="card"><div class="muted">PROVISIONAL / UNVERIFIED</div><div class="value">—</div></div></div><div class="scroll"><table><thead><tr><th>Rank</th><th>Symbol</th><th>Runner score</th><th>Latest tracked price</th><th>Price status</th><th>Premarket gap</th><th>Premarket RVOL</th><th>Premarket volume</th><th>Catalyst / dilution notes</th></tr></thead><tbody id="early-runner-rows"><tr><td colspan="9" class="empty">Loading Early Runner data…</td></tr></tbody></table></div><div class="tiny footnote">Research ranking only. Quotes retain their verified / unverified status; a high rank is not a BUY signal. This board reads saved hourly-scan history and does not trigger another scan.</div></div></div></details>
<section class="section" id="radar-timeline"><div class="section-heading"><div><div class="eyebrow">OPPORTUNITY LIFECYCLE</div><h2>Radar timeline</h2><div class="muted">A compact view of where candidates sit in the discovery-to-action lifecycle.</div></div><div class="tiny">Stage history is recorded by completed scans</div></div><div id="radar-timeline-cards" class="timeline-grid"><div class="op-empty">Waiting for candidate history…</div></div></section>
<details class="archive" id="performance-archive"><summary><span>Performance & tracking details</span><span class="summary-note">Scorecards, history, and full candidate tables</span></summary><div class="archive-body"><h2 class="archive-title">Performance snapshot</h2><div class="cards" id="stats"></div><section class="section" id="target-scorecard"><h2>Level 1 / Level 2 target scorecard</h2><div class="muted">First observed sampled-price crossings of saved BUY targets. These are observations, not guaranteed fills or intraday execution records.</div><div class="cards" id="target-performance"></div><div class="scroll"><table><thead><tr><th>Signal</th><th>Entry</th><th>Level 1</th><th>Level 2</th><th>Stop</th><th>Time to L1</th><th>Time to L2</th></tr></thead><tbody id="target-tracking"></tbody></table></div></section>
<section class="section"><h2>Stock vs. crypto scoreboard</h2><div class="grid" id="assets"></div></section>
<section class="section"><h2>Performance leaderboard</h2><div class="muted">Top tracked candidates by return from the first captured discovery price (provisional when initially unverified).</div><div class="scroll"><table><thead><tr><th>#</th><th>Candidate</th><th>Discovery price</th><th>Latest</th><th>Return</th><th>Best</th><th>Worst</th></tr></thead><tbody id="leaders"></tbody></table></div></section>
<section class="section" id="candidate-ledger"><h2>All tracked candidates</h2><div class="toolbar"><input id="q" placeholder="Search ticker…"><div class="action-filter-row" id="ledger-asset-filters" aria-label="Filter tracked candidates by asset type"><button type="button" class="action-filter active" data-ledger-asset="all" aria-pressed="true">ALL</button><button type="button" class="action-filter" data-ledger-asset="stock" aria-pressed="false">STOCKS</button><button type="button" class="action-filter" data-ledger-asset="crypto" aria-pressed="false">CRYPTO</button></div><select id="gradeFilter"><option value="all">All grades</option><option>BUY</option><option>ACTIONABLE SETUP</option><option>DEVELOPING RUNNER</option><option>DEVELOPING SETUP</option><option>EARLY SETUP</option><option>EARLY WATCH</option><option>LATE RUNNER — CHASE RISK</option><option>LOW-CONVICTION WATCH</option><option>PRICE UNVERIFIED — WATCH ONLY</option></select></div><div class="action-counts" id="ledger-candidate-counts" aria-live="polite">Loading candidate counts…</div><div class="scroll"><table><thead><tr><th>Candidate</th><th>First seen</th><th>Discovery price</th><th>Current grade</th><th>Price source / freshness</th><th>Price as of</th><th>Session move</th><th>Return since discovery</th><th>Best</th><th>Worst</th><th>Age</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="section"><h2>BUY signal performance</h2><div class="muted">Measured from the exact first BUY signal price. Outcomes are recorded at fixed elapsed-time checkpoints; this is a paper record, not executed fills.</div><div class="scroll"><table><thead><tr><th>Signal</th><th>First BUY</th><th>Entry</th><th>Latest return</th><th>1h</th><th>4h</th><th>1d</th><th>3d</th><th>5d</th><th>7d</th><th>10d</th><th>Score</th></tr></thead><tbody id="buy-history"></tbody></table></div></section><section class="section"><h2>Accuracy scorecard</h2><div id="ledger-integrity" class="muted">Ledger integrity will appear after data loads.</div><div class="muted">Current observed outcomes; legacy checkpoint totals may include stock observations made before independent quote validation.</div><div class="grid"><div class="panel"><h3>Return thresholds</h3><div id="thresholds"></div></div><div class="panel"><h3>Lifetime checkpoints</h3><div class="scroll"><table><thead><tr><th>Days</th><th>Observations</th><th>Wins</th><th>Losses</th><th>Average return</th></tr></thead><tbody id="checks"></tbody></table></div></div><div class="panel"><h3>BUY signal outcome cohorts</h3><div class="tiny">Matured signal outcomes measured on the first completed scan at or after each horizon.</div><div class="scroll"><table><thead><tr><th>Horizon</th><th>Observations</th><th>Wins</th><th>Losses</th><th>Average return</th></tr></thead><tbody id="signal-checks"></tbody></table></div></div></div></section>
<section class="section"><h2>Grade movement history</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>From</th><th>To</th><th>When</th></tr></thead><tbody id="moves"></tbody></table></div></section>
<section class="section"><h2>Discovery reasons & catalysts</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>Signals</th><th>Latest headline / filing</th><th>Risk flags</th></tr></thead><tbody id="catalysts"></tbody></table></div></section>
<section class="section"><h2>Not in latest scan</h2><div class="tiny">Previously tracked, but absent from the latest scan. Last recorded prices may be stale; this is not a sell signal.</div><div class="scroll"><table><thead><tr><th>Candidate</th><th>Last seen</th><th>Last return</th><th>Best gain</th><th>Last grade</th></tr></thead><tbody id="faded"></tbody></table></div></section>
<p class="tiny section">Data may be delayed or incomplete. Candidate discovery is not the same as an actionable alert.</p></div></details></main><div hidden aria-hidden="true"><div id="intelligence-scorecard"></div><span id="discovery-count"></span><div id="discovery-tracker"></div><span id="momentum-count"></span><div id="momentum-monitor"></div><div id="discovery-heatmap"></div><div id="heatmap-footnote"></div></div><script src="/history/dashboard.js?v=20261119-3"></script></body></html>""",headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"})
 
 


HISTORY_DASHBOARD_JS = r"""

window.addEventListener('error',function(ev){const el=document.getElementById('updated');if(el)el.textContent='Overview JavaScript error: '+(ev.message||'script error');});
window.addEventListener('unhandledrejection',function(ev){const el=document.getElementById('updated');if(el)el.textContent='Overview promise error: '+(ev.reason&&ev.reason.message||ev.reason||'unknown');});
const bootStatus=document.getElementById('updated');if(bootStatus)bootStatus.textContent='Dashboard script started · loading saved history…';
(async function loadHistoryDashboard(){
 const status=document.getElementById('updated');
 const controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),25000);
 try{
  const response=await fetch('/history/data?refresh='+Date.now(),{cache:'no-store',signal:controller.signal});
  if(!response.ok)throw Error('History request failed: HTTP '+response.status);
  const data=await response.json();
  if(!data||typeof data.paper_tracker!=='object'||data.paper_tracker===null)throw Error('History API returned an invalid candidate ledger');
  const recordCount=Object.keys(data.paper_tracker).length;
  // Update and paint the connection state before the heavier DOM/table rendering work.
  if(status)status.textContent='Saved history received · preparing '+recordCount+' tracked records…';
  await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
  try{
   render(data);
   const snapshotTime=data.last_scan_completed_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(data.last_scan_completed_at)):'timestamp unavailable';
   if(status)status.textContent='Dashboard loaded · '+recordCount+' tracked records · Data snapshot: '+snapshotTime;
  }catch(err){
   console.error('Jarvis history render error:',err);
   if(status)status.textContent='Dashboard rendering failed: '+(err&&err.message||err);
  }
 }catch(err){
  console.error('Jarvis history data load error:',err);
  if(status)status.textContent=(err&&err.name==='AbortError')?'History data timed out · reload to retry':'History data load failed: '+(err&&err.message||err);
  const pick=document.getElementById('action-top-pick');
  if(pick)pick.innerHTML='<div><div class="action-pick-title">TOP RADAR CANDIDATE</div><div class="action-pick-symbol">Dashboard data unavailable</div><div class="action-pick-reason">The saved scan could not be loaded. Reload to retry; no trade signal is implied.</div></div><span class="action-pick-status">DATA UNAVAILABLE</span>';
 }finally{clearTimeout(timeout);}
})()
const status=document.getElementById('updated');if(status)status.textContent='Dashboard loading…';
const n=v=>Number(v||0),hasPct=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v)),pct=v=>hasPct(v)?Number(v).toFixed(2)+'%':'—',cl=v=>!hasPct(v)?'':n(v)>0?'pos':n(v)<0?'neg':'',e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"),date=v=>v?new Date(v).toLocaleString():'—';
const rank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1};
const PREF_KEY='jarvisDashboardPrefsV1';const prefs={radarFilter:'all',assetFilter:'all',density:'comfortable',showTimeline:true,showPerformance:true,accent:'#4aa3df',font:'Inter,system-ui,sans-serif',fontSize:'14'};try{Object.assign(prefs,JSON.parse(localStorage.getItem(PREF_KEY)||'{}'));}catch(_){}
function applyPrefs(){document.body.classList.toggle('compact-mode',prefs.density==='compact');document.documentElement.style.setProperty('--accent',prefs.accent||'#4aa3df');document.body.style.fontFamily=prefs.font||'Inter,system-ui,sans-serif';document.body.style.fontSize=(Number(prefs.fontSize)||14)+'px';const timeline=document.getElementById('radar-timeline'),archive=document.getElementById('performance-archive');if(timeline)timeline.hidden=!prefs.showTimeline;if(archive)archive.hidden=!prefs.showPerformance;}
function savePrefs(){try{localStorage.setItem(PREF_KEY,JSON.stringify(prefs));}catch(_){}applyPrefs();}
function bindPrefs(){const radar=document.getElementById('radar-filter'),assetFilter=document.getElementById('radar-asset-filter'),density=document.getElementById('layout-density'),timeline=document.getElementById('show-timeline'),performance=document.getElementById('show-performance');if(radar&&assetFilter&&density&&timeline&&performance){radar.value=prefs.radarFilter;assetFilter.value=prefs.assetFilter||'all';density.value=prefs.density;timeline.checked=prefs.showTimeline;performance.checked=prefs.showPerformance;radar.addEventListener('change',()=>{prefs.radarFilter=radar.value;savePrefs();if(window.jarvisRenderData)render(window.jarvisRenderData);});assetFilter.addEventListener('change',()=>{prefs.assetFilter=assetFilter.value;savePrefs();if(window.jarvisRenderData)render(window.jarvisRenderData);});density.addEventListener('change',()=>{prefs.density=density.value;savePrefs();});timeline.addEventListener('change',()=>{prefs.showTimeline=timeline.checked;savePrefs();});performance.addEventListener('change',()=>{prefs.showPerformance=performance.checked;savePrefs();});}applyPrefs();}
document.addEventListener('DOMContentLoaded',bindPrefs);


function elapsed(from,to){if(!from||!to)return '—';const ms=new Date(to)-new Date(from);if(!Number.isFinite(ms)||ms<0)return '—';const hours=Math.floor(ms/3600000);return hours<24?hours+'h':Math.floor(hours/24)+'d '+Math.floor(hours%24)+'h';}const etStamp=v=>v?new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}).format(new Date(v)):'—';function priceTiming(x){const lastKnown=x.price_validation_status==='last_known'||x.price_timestamp_type==='daily_session_close';const sessionDate=x.price_session_date||(lastKnown&&x.price_as_of?new Date(x.price_as_of).toISOString().slice(0,10):null);const priceTime=lastKnown?(sessionDate?sessionDate+' · regular close scheduled 4:00 PM ET':'Daily session close · date unavailable'):etStamp(x.price_as_of);const checked=etStamp(x.price_checked_at||x.last_seen||(x.data_quality&&x.data_quality.received_at));const note=lastKnown?'Daily-bar source provides the session date, not the exact final-trade timestamp; early closes can differ.':'Exchange/provider timestamp';const status=lastKnown?'LAST KNOWN · EOD':x.price_validation_status==='verified'?'LIVE VERIFIED':'UNVERIFIED';const baseStatus=x.discovery_baseline_status==='last_known_eod'?'LAST KNOWN EOD':x.discovery_baseline_status==='live_verified'?'LIVE VERIFIED':x.discovery_baseline_status==='first_verified_legacy'?'LEGACY FIRST-VERIFIED':x.discovery_baseline_status==='first_verified_fallback'?'FIRST VERIFIED FALLBACK':x.discovery_baseline_status==='unverified_at_discovery'?'FIRST CAPTURE · PROVISIONAL':'AWAITING RELIABLE PRICE';const baseTime=x.discovery_baseline_timestamp_type==='daily_session_close'?String(x.discovery_price_as_of||'').slice(0,10):etStamp(x.discovery_price_as_of);return '<div class="tiny"><b>'+e(status)+'</b></div><div class="tiny">PRICE TIME: '+e(priceTime)+'</div><div class="tiny">JARVIS CHECKED: '+e(checked)+'</div><div class="tiny">DISCOVERY BASELINE: '+e(baseStatus)+' · '+e(baseTime||'date unavailable')+'</div><div class="tiny">'+e(note)+'</div>';}function render(d){window.jarvisRenderData=d;const t=d.paper_tracker||{},l=d.lifetime||{},all=Object.values(t),buySignals=d.buy_signals||[],activeBuys=buySignals.filter(x=>x.status==="active"),verified=all.filter(x=>x.price_validation_status==="verified"),trackedReturns=all.filter(x=>hasPct(x.discovery_return_pct)),live=all.filter(x=>x.currently_surfaced!==false),avg=trackedReturns.length?trackedReturns.reduce((s,x)=>s+n(x.discovery_return_pct),0)/trackedReturns.length:null;
const integrity=l.integrity||{};const integrityNode=document.getElementById('ledger-integrity');if(integrityNode){const missing=n(integrity.retained_stock_uncounted_baselines);integrityNode.textContent='Ledger integrity · stock baselines '+n(integrity.retained_stock_baselines)+' · counted '+n(integrity.retained_stock_counted_baselines)+' · uncounted '+missing+' · unverified without baseline '+n(integrity.unverified_stock_without_baseline)+(missing?' · CHECK REQUIRED':' · OK');}
const fmtTime=d.last_scan_completed_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(d.last_scan_completed_at)):'No completed scan timestamp yet';document.getElementById('updated').textContent='Last scan completed: '+fmtTime;

const stageRank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1};
// Discovery radar retains every successfully scanned candidate; price verification controls entry eligibility, not visibility.
const surfaced=all.filter(x=>x.currently_surfaced!==false);
const diagnosticUnverified=all.filter(x=>x.currently_surfaced!==false&&x.price_validation_status==='unverified');
const earlyCount=surfaced.filter(x=>['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(x.last_stage)).length;const actionableCount=surfaced.filter(x=>x.last_stage==='ACTIONABLE SETUP').length;
const stockVerified=surfaced.filter(x=>x.asset_type==='stock'&&x.price_validation_status==='verified').length;
const stockUnverified=diagnosticUnverified.filter(x=>x.asset_type==='stock').length;
const cryptoVerified=surfaced.filter(x=>x.asset_type==='crypto'&&x.price_validation_status==='verified').length;
const cryptoUnverified=diagnosticUnverified.filter(x=>x.asset_type==='crypto').length;
const radarStatsNode=document.getElementById('radar-stats');if(radarStatsNode)radarStatsNode.innerHTML=[[''+activeBuys.length,'ACTIVE BUY SIGNALS'],[''+surfaced.length,'ON RADAR'],[''+earlyCount,'EARLY / WATCH-ONLY'],[''+actionableCount,'ACTIONABLE SETUPS'],[''+stockVerified,'STOCKS PRICE VERIFIED'],[''+stockUnverified,'STOCKS UNVERIFIED · WATCH ONLY'],[''+cryptoVerified,'CRYPTO PRICE VERIFIED'],[''+cryptoUnverified,'CRYPTO UNVERIFIED · WATCH ONLY']].map(x=>'<div class="radar-stat"><strong>'+e(x[0])+'</strong><span>'+x[1]+'</span></div>').join('');


const radarPool=surfaced.filter(x=>(prefs.assetFilter==='all'||x.asset_type===prefs.assetFilter)&&(prefs.radarFilter==='all'||(prefs.radarFilter==='developing'&&['BUY','ACTIONABLE SETUP','DEVELOPING RUNNER','DEVELOPING SETUP','EARLY SETUP'].includes(x.last_stage))||(prefs.radarFilter==='actionable'&&['BUY','ACTIONABLE SETUP'].includes(x.last_stage))));const rankRadar=(a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)||n(b.daily_change_pct)-n(a.daily_change_pct);const rankedRadar=radarPool.sort(rankRadar);const candidates=[...rankedRadar.filter(x=>x.asset_type==='stock').slice(0,9),...rankedRadar.filter(x=>x.asset_type==='crypto').slice(0,9)].sort(rankRadar);
document.getElementById('opportunities').innerHTML=candidates.length?candidates.map((x,i)=>{
 const stage=x.last_stage||'WATCHLIST',s=String(stage),kind=s.includes('ACTIONABLE')?'action':s.includes('LATE RUNNER')?'late':'';
 const reason=(x.stage_reasons||x.discovery_reasons||[]).slice(0,2).join(' · ')||'Candidate retained for continued monitoring.';
 const risks=(x.risk_flags||[]).slice(0,2).join(' · ');
 const price=x.latest_price==null?'—':(Number.isFinite(Number(x.latest_price))?Number(x.latest_price).toPrecision(7):'—');
 const day=x.daily_change_pct==null?'—':pct(x.daily_change_pct);
 const ret=hasPct(x.discovery_return_pct)?(pct(x.discovery_return_pct)+(x.discovery_return_status==='provisional'?' · PROVISIONAL':'')):(x.discovery_baseline_status==='first_verified_fallback'?'From first verified':(x.price_validation_status==='unverified'?'Awaiting first usable quote':'—'));
 const discoveryCaptured=x.discovery_price_checked_at||x.first_seen||null;
 const discoveryPriceAsOf=x.discovery_price_as_of||null;
 const discoveryTime=discoveryCaptured?new Date(discoveryCaptured).toLocaleString():'Not recorded';
 const discoveryAsOf=discoveryPriceAsOf?new Date(discoveryPriceAsOf).toLocaleString():null;
 const lv=x.levels||{}, verified=x.price_validation_status==='verified';
 const earlySignals=(x.stage_signals&&x.stage_signals.early)||[],lateSignals=(x.stage_signals&&x.stage_signals.late)||[];
 const riskFlags=x.risk_flags||[],components=x.score_components||{},levels=x.levels||{};
 const clamp=(v,max)=>Math.max(0,Math.min(1,(Number(v)||0)/max));
 const stock=x.asset_type==='stock';
 const momentum=stock?((clamp(components.momentum,25)+clamp(components.trend,15))/2)*20:clamp(components.momentum,30)*20;
 const volume=clamp(components.volume,25)*15;
 const breakout=clamp(components.breakout,stock?20:25)*15;
 const technical=stock?clamp(components.technical_confirmation,15)*25:clamp(components.trend,20)*25;
 const rr=Number(levels.reward_risk_ratio||0),riskPct=Number(levels.risk_pct);
 const riskReward=rr>=2&&Number.isFinite(riskPct)?(riskPct<=5?15:riskPct<=8?12:riskPct<=10?9:4):0;
 const activeFamilies=[components.momentum,components.volume,components.breakout,components.trend,...(stock?[components.technical_confirmation]:[])].filter(v=>Number(v)>0).length;
 const agreement=Math.min(10,activeFamilies/(stock?5:4)*10);
 const riskPenalty=Math.min(16,riskFlags.reduce((sum,flag)=>sum+( /offering|dilution|registration|financing/i.test(String(flag))?6:4),0));
 const latePenalty=s.includes('LATE RUNNER')?12:0;
 const regimePenalty=x.market_regime==='RISK-OFF'?8:0;
 const qualityParts={momentum_trend:Math.round(momentum),volume:Math.round(volume),breakout:Math.round(breakout),technical_confirmation:Math.round(technical),risk_reward:Math.round(riskReward),signal_agreement:Math.round(agreement)};
 const eodProvisional=stock&&x.price_validation_status==='last_known';
 const evidenceAvailable=stock&&['momentum','volume','breakout','trend','technical_confirmation'].some(k=>components[k]!==undefined&&components[k]!==null&&Number.isFinite(Number(components[k])));
 const evidenceProvisional=stock&&!verified&&!eodProvisional&&evidenceAvailable;
 const quality=(verified||eodProvisional||evidenceAvailable)?Math.max(0,Math.min(100,Math.round(Object.values(qualityParts).reduce((a,b)=>a+b,0)-riskPenalty-latePenalty-regimePenalty))):null;
 const confidence=quality;
 const confidenceLabel=confidence===null?'Unavailable':confidence>=80?'Strong':confidence>=65?'Good':confidence>=50?'Developing':'Weak';
 const qualityBreakdown=quality===null?'Price verification required':(stock?'Momentum/trend '+qualityParts.momentum_trend+'/20':'Momentum '+qualityParts.momentum_trend+'/20')+' · Volume '+qualityParts.volume+'/15 · Breakout '+qualityParts.breakout+'/15 · '+(stock?'Technical':'Trend')+' '+qualityParts.technical_confirmation+'/25 · Risk/reward '+qualityParts.risk_reward+'/15 · Agreement '+qualityParts.signal_agreement+'/10'+(eodProvisional?' · EOD-based provisional estimate; not live entry confirmation':'');
 const fmtLevel=v=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toPrecision(7);
 const entry=lv.watch_entry_low!=null?fmtLevel(lv.watch_entry_low)+' – '+fmtLevel(lv.watch_entry_high):'Not available';
 const plan=verified&&lv.target_1!=null&&lv.target_2!=null;
 const calc=lv.calculation||{},riskPer=calc.risk_per_unit!=null?Number(calc.risk_per_unit):((Number(lv.watch_entry_high)||0)-(Number(lv.stop_loss||lv.invalidation_level)||0));
 const entryRef=Number(lv.watch_entry_high)||0,stopRef=Number(lv.stop_loss||lv.invalidation_level)||0;
 const fmtOrDash=v=>v==null||!Number.isFinite(Number(v))?'—':fmtLevel(v);
 const calcLine=plan?'<section class="target-math"><div class="target-math-head"><b>TARGET MATH</b><span>2R / 3R MODEL</span></div><div class="target-math-inputs"><div class="target-math-input"><span>ATR · 14 PERIOD</span><strong>'+e(calc.atr_14==null?'Not saved':fmtOrDash(calc.atr_14))+'</strong></div><div class="target-math-input"><span>RISK UNIT</span><strong>'+e(fmtOrDash(calc.risk_unit))+'</strong></div><div class="target-math-input"><span>ENTRY REFERENCE · HIGH</span><strong>'+e(fmtOrDash(entryRef))+'</strong></div><div class="target-math-input"><span>STOP · '+e((calc.stop_source||'SAVED STOP').toUpperCase())+'</span><strong>'+e(fmtOrDash(stopRef))+'</strong></div></div><div class="target-math-risk"><span>RISK PER SHARE / COIN<br>Entry reference − stop</span><strong>'+e(fmtOrDash(riskPer))+'</strong></div><div class="target-math-level"><div><small>LEVEL 1 · 2R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (2 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_1))+'</strong><span>PRICE TARGET · '+e(entryRef>0&&Number.isFinite(Number(lv.target_1))?((Number(lv.target_1)/entryRef-1)*100>=0?'+':'')+((Number(lv.target_1)/entryRef-1)*100).toFixed(2)+'% FROM RANGE HIGH':'GAIN N/A')+'</span></div></div><div class="target-math-level level-two"><div><small>LEVEL 2 · 3R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (3 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_2))+'</strong><span>PRICE TARGET · '+e(entryRef>0&&Number.isFinite(Number(lv.target_2))?((Number(lv.target_2)/entryRef-1)*100>=0?'+':'')+((Number(lv.target_2)/entryRef-1)*100).toFixed(2)+'% FROM RANGE HIGH':'GAIN N/A')+'</span></div></div><div class="target-exit-note" style="margin:12px 0;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:12px;line-height:1.55"><b>PAPER EXIT RULES</b><br>Level 1: sell 50% of the paper position at the first verified scan price at or above this target. Level 2: sell the remaining 50% at the first verified scan price at or above this target. If the stop is reached first, exit the full position. These are scan-observed simulated exits, not guaranteed fills at the exact card price.</div><div class="target-math-foot"><b>How inputs are set:</b> Risk unit = greater of ATR(14) or 2% of signal price. Entry reference = upper entry range. Initial stop distance = 1.5 × risk unit, subject to a 10% price cap and a valid nearby-support adjustment. '+(calc.atr_14==null?'ATR inputs were not retained for this older saved plan.':'')+'</div></section>':'';



 const confidenceWhy=confidence===null?'Not enough saved technical evidence to calculate a setup score.':qualityBreakdown+'. Deductions: '+(riskPenalty+latePenalty+regimePenalty)+' points for risk flags, late-stage extension and/or risk-off conditions. Setup Quality measures how strongly the available signals support the setup; it is not the probability of making a profit.'+(eodProvisional?' EOD-BASED · PROVISIONAL: calculated from the cross-checked prior-session close and saved indicators; wait for live price confirmation before considering an entry.':evidenceProvisional?' PROVISIONAL · PRICE PENDING: calculated from saved technical indicators only; price verification is still required before any BUY or trade plan.':'');
 const evidenceItems=[['MOMENTUM',qualityParts.momentum_trend,20],['VOLUME',qualityParts.volume,15],['BREAKOUT',qualityParts.breakout,15],[stock?'TECHNICAL':'TREND',qualityParts.technical_confirmation,25],['RISK / REWARD',qualityParts.risk_reward,15],['AGREEMENT',qualityParts.signal_agreement,10]];
 const evidenceHtml=evidenceItems.map(([label,score,max])=>'<div class="op-evidence-item"><div><span>'+label+'</span><b>'+score+'/'+max+'</b></div><i><em style="width:'+Math.max(0,Math.min(100,score/max*100))+'%"></em></i></div>').join('');
 const frontQuality=stock?confidence===null?'<div class="op-front-quality unavailable"><div><span>SETUP QUALITY</span><strong>Insufficient data</strong></div><small>Technical evidence not available for calculation</small></div>':'<div class="op-front-quality"><div class="op-front-quality-top"><div><span>SETUP QUALITY · '+(eodProvisional?'EOD-BASED · PROVISIONAL':evidenceProvisional?'PROVISIONAL · PRICE PENDING':'EVIDENCE SCORE')+'</span><strong>'+confidence+'<small>/100</small></strong></div><span class="op-quality-label">'+e(confidenceLabel.toUpperCase())+'</span></div><div class="op-quality-track"><i style="width:'+confidence+'%"></i></div><div class="op-evidence-grid">'+evidenceHtml+'</div>'+(eodProvisional?'<small>EOD research score only · BUY and trade-plan gates still require live verification</small>':evidenceProvisional?'<small>Provisional research score from saved technical evidence · BUY and trade-plan gates still require independent live price verification</small>':'')+'</div>' :'';
 const frontPlan=plan?'<div class="op-front-plan"><div><span>ENTRY ZONE</span><b>'+e(entry)+'</b></div><div><span>STOP / INVALIDATION</span><b>'+e(fmtLevel(lv.stop_loss||lv.invalidation_level))+'</b></div></div>':(verified?'':'<div class="op-front-plan-note">Price unverified · trade levels withheld</div>');
 const frontExplanation=stock?'<div class="op-front-explanation"><div>'+e(confidenceWhy)+'</div><div>Method: '+e(lv.method||'ATR-based stop and 2R / 3R reward-to-risk targets where saved.')+'</div></div>' :'';

 return '<article data-asset-type="'+e(x.asset_type||'')+'" data-symbol="'+e(x.symbol||'')+'" data-stage="'+e(stage)+'" class="op-card op-flippable '+(i===0?'featured':'')+'" tabindex="0" role="button" aria-pressed="false" aria-label="Flip '+e(x.symbol||'candidate')+' opportunity card for targets and confidence"><div class="op-flip-inner"><div class="op-face op-face-front"><div class="op-card-top"><div><div class="op-kind">'+(i===0?'TOP RADAR CANDIDATE · ':'')+e((x.asset_type||'').toUpperCase())+'</div><div class="op-symbol">'+e(x.symbol||'—')+'</div></div><span class="op-stage '+kind+'">'+e(stage)+'</span></div><div class="op-metrics"><div class="op-metric"><strong>'+e(price)+'</strong><span>LATEST PRICE</span></div><div class="op-metric"><strong class="'+cl(x.daily_change_pct)+'">'+e(day)+'</strong><span>24H MOVE</span></div><div class="op-metric"><strong class="'+cl(x.discovery_return_pct)+'">'+e(ret)+'</strong><span>RETURN SINCE DISCOVERY</span></div></div><div class="op-discovery-time" style="font-size:11px;line-height:1.5;color:var(--muted);margin-top:-7px;margin-bottom:10px">FIRST CAPTURED: <b>'+e(discoveryTime)+'</b>'+(discoveryAsOf?' · PRICE AS OF: <b>'+e(discoveryAsOf)+'</b>':'')+'</div>'+(stock?(confidence===null?'<div class="op-quick-quality"><span>SETUP QUALITY</span><strong>Pending verification</strong></div>':'<div class="op-quick-quality"><span>SETUP QUALITY</span><strong>'+confidence+'/100</strong><em>'+e(confidenceLabel.toUpperCase())+'</em></div>'):'<div class="op-quick-quality"><span>CRYPTO RUNNER QUALITY</span><strong>'+e(x.crqs==null?'—':x.crqs)+'/74</strong><em>'+e(x.crqs_tier||'Score unavailable')+'</em></div>')+frontPlan+'<div class="op-reason"><b>WHY IT\'S ON RADAR</b><br>'+e(reason)+'</div><details class="op-details"><summary>Full analysis · price history · scoring · risks</summary><div class="op-details-body">'+priceTiming(x)+frontQuality+(stock?'':'<div class="op-front-quality"><div><span>CRYPTO RUNNER QUALITY · CRQS</span><strong>'+e(x.crqs==null?'—':x.crqs)+'<small>/74</small></strong></div><span class="op-quality-label">'+e(x.crqs_tier||'Score unavailable')+'</span><small>Entry timing score: '+e(x.crqs_entry_score==null?'—':x.crqs_entry_score)+'/74 · Separate from BUY signal</small><small>74-point calibrated ceiling; relative strength, order-book depth/spread and token-unlock risk are not scored yet.</small></div>')+frontExplanation+(risks?'<div class="op-risk">RISK FLAGS: '+e(risks)+'</div>':'')+'</div></details><div class="op-flip-hint">TAP CARD FOR TARGETS + STOP LOGIC ↻</div></div><div class="op-face op-face-back"><div class="op-back-title">'+e((x.symbol||'CANDIDATE')+' · POTENTIAL TRADE PLAN')+'</div>'+calcLine+'<div class="op-flip-hint">TAP TO RETURN TO OPPORTUNITY ↻</div></div></div></article>';
}).join(''):'<div class="op-empty">Nothing has qualified for the radar yet. Jarvis will keep monitoring and show candidates after a successful scan.</div>';

const commandMetrics=document.getElementById('command-metrics');
if(commandMetrics){const verifiedCount=surfaced.filter(x=>x.price_validation_status==='verified').length;const combinedActionable=activeBuys.length+actionableCount;commandMetrics.innerHTML=[[''+all.length,'TRACKED ASSETS'],[''+surfaced.length,'ON RADAR NOW'],[''+earlyCount,'EARLY / BUILDING'],[''+combinedActionable,'BUY + ACTIONABLE']].map(([v,label])=>'<div class="command-metric"><strong>'+e(v)+'</strong><span>'+label+'</span></div>').join('');}
const commandScan=document.getElementById('command-scan-status');if(commandScan)commandScan.textContent='Last completed scan · '+fmtTime;
const commandData=document.getElementById('command-data-status');if(commandData){const verifiedCount=surfaced.filter(x=>x.price_validation_status==='verified').length;commandData.textContent='Verified prices '+verifiedCount+' / '+surfaced.length+' · unverified assets remain visible for research only';}
const runnerLaneDefs=[
 {key:'emerging',title:'EMERGING',desc:'Early watch and early setup',stages:['EARLY WATCH','EARLY SETUP']},
 {key:'building',title:'BUILDING',desc:'Developing setup and runner',stages:['DEVELOPING SETUP','DEVELOPING RUNNER']},
 {key:'actionable',title:'ACTIONABLE',desc:'Qualified setup or BUY stage',stages:['ACTIONABLE SETUP','BUY']},
 {key:'cooling',title:'COOLING / RISK',desc:'Late extension or lower conviction',stages:['LATE RUNNER — CHASE RISK','LOW-CONVICTION WATCH']}
];
const runnerLanes=document.getElementById('runner-radar-lanes');
if(runnerLanes){runnerLanes.innerHTML=runnerLaneDefs.map(lane=>{const items=candidates.filter(x=>lane.stages.includes(String(x.last_stage||'').toUpperCase()));const chips=items.slice(0,5).map(x=>'<button type="button" class="runner-chip" data-runner-symbol="'+e(x.symbol||'')+'" data-runner-asset="'+e(x.asset_type||'')+'">'+e((x.asset_type||'').toUpperCase()+' · '+(x.symbol||'—'))+'</button>').join('');return '<div class="runner-lane"><div class="runner-lane-top"><span class="runner-lane-title">'+lane.title+'</span><span class="runner-lane-count">'+items.length+'</span></div><p>'+lane.desc+'</p><div class="runner-chip-list">'+(chips||'<span class="runner-empty">No candidates in this stage</span>')+'</div></div>';}).join('');}

// Performance & Momentum Lab: presentation-only calculations over the saved ledger.
const measured=trackedReturns.filter(x=>x.discovery_initial_price!=null&&Number.isFinite(Number(x.discovery_initial_price))&&Number(x.discovery_initial_price)>0);
const positive=measured.filter(x=>n(x.discovery_return_pct)>0);
const sortedMeasured=[...measured].sort((a,b)=>n(b.discovery_return_pct)-n(a.discovery_return_pct));
const bestMeasured=sortedMeasured[0];
const averageMeasured=measured.length?measured.reduce((sum,x)=>sum+n(x.discovery_return_pct),0)/measured.length:null;
const scoreNode=document.getElementById('intelligence-scorecard');
if(scoreNode){const vals=[[measured.length,'BASELINES TRACKED'],[positive.length+' / '+measured.length,'POSITIVE RETURNS'],[averageMeasured===null?'—':pct(averageMeasured),'AVERAGE RETURN'],[bestMeasured?e(bestMeasured.symbol)+' · '+pct(bestMeasured.discovery_return_pct):'—','BEST DISCOVERY']];scoreNode.innerHTML=vals.map(([v,label])=>'<div class="analytics-score"><strong>'+v+'</strong><span>'+label+'</span></div>').join('');}
const discoveryNode=document.getElementById('discovery-tracker');
const discoveryCount=document.getElementById('discovery-count');
if(discoveryCount)discoveryCount.textContent=measured.length+' BASELINES';
if(discoveryNode){const list=sortedMeasured.slice(0,8);discoveryNode.innerHTML=list.length?list.map(x=>{const ret=n(x.discovery_return_pct),max=hasPct(x.discovery_max_gain_pct)?n(x.discovery_max_gain_pct):null,base=x.discovery_initial_price;const captured=x.first_seen||x.discovery_price_as_of;const asOf=x.discovery_price_as_of;return '<div class="discovery-row"><div><div class="analytics-asset">'+e((x.asset_type||'').toUpperCase()+' · '+x.symbol)+'</div><div class="analytics-sub">BASE '+e(Number(base).toPrecision(7))+' · '+e(captured?date(captured):'Discovery time unavailable')+'</div><div class="analytics-sub">PRICE AS OF '+e(asOf?date(asOf):'Timestamp unavailable')+' · '+e(x.discovery_baseline_status||'baseline status unavailable')+'</div><div class="analytics-meter"><i style="width:'+Math.max(0,Math.min(100,max===null?Math.abs(ret):Math.max(0,Math.min(100,max))))+'%"></i></div></div><div class="analytics-return '+cl(ret)+'">'+pct(ret)+(max===null?'':'<div class="analytics-sub">BEST '+pct(max)+'</div>')+'</div></div>';}).join(''):'<div class="runner-empty">No eligible discovery baselines have return measurements yet.</div>';}
const momentumPool=surfaced.map(x=>{const daily=hasPct(x.daily_change_pct)?n(x.daily_change_pct):null;const comps=x.score_components||{};const mom=hasPct(comps.momentum)?n(comps.momentum):null;const vol=hasPct(comps.volume)?n(comps.volume):null;const rvol=hasPct(x.premarket_rvol_same_time)?n(x.premarket_rvol_same_time):null;const available=[daily!==null,mom!==null,vol!==null,rvol!==null].filter(Boolean).length;const evidence=(daily===null?0:Math.max(-20,Math.min(20,daily))*2)+(mom===null?0:Math.max(0,Math.min(25,mom)))+(vol===null?0:Math.max(0,Math.min(25,vol)))+(rvol===null?0:Math.max(0,Math.min(5,rvol))*2);return {...x,_momentumEvidence:available?evidence:null,_momentumFields:{daily,mom,vol,rvol,available}};}).filter(x=>x._momentumEvidence!==null).sort((a,b)=>b._momentumEvidence-a._momentumEvidence);
const momentumNode=document.getElementById('momentum-monitor'),momentumCount=document.getElementById('momentum-count');
if(momentumCount)momentumCount.textContent=momentumPool.length+' WITH EVIDENCE';
if(momentumNode){momentumNode.innerHTML=momentumPool.slice(0,8).map(x=>{const m=x._momentumFields,day=m.daily===null?'DAILY MOVE N/A':pct(m.daily),rvol=m.rvol===null?'RVOL N/A':m.rvol.toFixed(2)+'x RVOL';return '<div class="momentum-row"><div><div class="analytics-asset">'+e((x.asset_type||'').toUpperCase()+' · '+x.symbol)+'</div><div class="analytics-sub">'+e(x.last_stage||'WATCH')+' · '+e(day)+' · '+e(rvol)+'</div><div class="analytics-sub">Saved momentum '+e(m.mom===null?'—':m.mom)+' · volume '+e(m.vol===null?'—':m.vol)+' · '+m.available+'/4 evidence fields</div></div><div class="analytics-return">'+e(Math.round(x._momentumEvidence))+'<div class="analytics-sub">EVIDENCE INDEX</div></div></div>';}).join('')||'<div class="runner-empty">No saved momentum / volume evidence is available yet.</div>';}
const heatNode=document.getElementById('discovery-heatmap');
if(heatNode){const heatItems=[...surfaced].sort((a,b)=>{const av=hasPct(a.daily_change_pct)?n(a.daily_change_pct):hasPct(a.discovery_return_pct)?n(a.discovery_return_pct):null,bv=hasPct(b.daily_change_pct)?n(b.daily_change_pct):hasPct(b.discovery_return_pct)?n(b.discovery_return_pct):null;return (bv??-Infinity)-(av??-Infinity);}).slice(0,36);heatNode.innerHTML=heatItems.length?heatItems.map(x=>{const daily=hasPct(x.daily_change_pct)?n(x.daily_change_pct):null,ret=hasPct(x.discovery_return_pct)?n(x.discovery_return_pct):null,value=daily===null?ret:daily,source=daily===null?(ret===null?'NO RETURN':'SINCE DISCOVERY'):'DAILY MOVE',state=value===null?'neutral':value>0?'positive':value<0?'negative':'neutral';return '<div class="heat-tile '+state+'"><strong>'+e(x.symbol||'—')+'</strong><b>'+(value===null?'—':pct(value))+'</b><small>'+e((x.asset_type||'').toUpperCase())+' · '+source+(x.price_validation_status==='unverified'?' · UNVERIFIED':'')+'</small></div>';}).join(''):'<div class="runner-empty">No saved assets to display.</div>';}
const heatFoot=document.getElementById('heatmap-footnote');if(heatFoot)heatFoot.textContent='Showing '+Math.min(36,surfaced.length)+' of '+surfaced.length+' surfaced assets · verified '+surfaced.filter(x=>x.price_validation_status==='verified').length+' · last known '+surfaced.filter(x=>x.price_validation_status==='last_known').length+' · unverified '+surfaced.filter(x=>x.price_validation_status==='unverified').length+'.';
document.querySelectorAll('[data-runner-symbol]').forEach(btn=>btn.addEventListener('click',()=>{const symbol=btn.dataset.runnerSymbol,asset=btn.dataset.runnerAsset;const card=[...document.querySelectorAll('.op-flippable')].find(c=>(c.dataset.symbol||'')===symbol&&(c.dataset.assetType||'')===asset);if(card){const library=document.getElementById('candidate-analysis-library');if(library)library.open=true;card.scrollIntoView({behavior:'smooth',block:'center'});if(!card.classList.contains('is-flipped'))card.click();}}));
function updateActionTopPick(){const actionPick=document.getElementById('action-top-pick');if(!actionPick)return;const filteredCandidates=candidates.filter(x=>{const asset=(x.asset_type||'').toLowerCase(),stage=(x.last_stage||'WATCH').toUpperCase();const assetOK=jAsset==='all'||asset===jAsset;let stageOK=true;if(jStage==='buy')stageOK=stage==='BUY';else if(jStage==='setup')stageOK=['ACTIONABLE SETUP','DEVELOPING SETUP','EARLY SETUP'].includes(stage);else if(jStage==='early')stageOK=['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);else if(jStage==='watch')stageOK=!['BUY','ACTIONABLE SETUP','EARLY SETUP','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);return assetOK&&stageOK;});if(filteredCandidates.length){const top=filteredCandidates[0],stage=top.last_stage||'WATCH',kind=(top.asset_type||'').toUpperCase(),sym=top.symbol||'—';const stageLabel=stage==='BUY'?'BUY SIGNAL':stage==='ACTIONABLE SETUP'?'ACTIONABLE SETUP':stage.includes('EARLY')||stage.includes('RUNNER')?'EARLY RUNNER':'WATCH / DEVELOPING';actionPick.dataset.asset=String(top.asset_type||'').toLowerCase();actionPick.dataset.symbol=String(sym);actionPick.setAttribute('aria-label','Open full analysis for '+kind+' '+sym);actionPick.innerHTML='<div><div class="action-pick-title">TOP-RANKED OPPORTUNITY · '+e(kind)+'</div><div class="action-pick-symbol">'+e(sym)+'</div><div class="action-pick-reason">'+e((top.stage_reasons||top.discovery_reasons||[]).slice(0,3).join(' · ')||'Ranked first by the existing Jarvis radar.')+'</div><span class="action-pick-status">'+e(stageLabel)+'</span></div><span class="action-pick-status">OPEN FULL ANALYSIS ↓</span>';}else{delete actionPick.dataset.asset;delete actionPick.dataset.symbol;actionPick.setAttribute('aria-label','No candidate matches the selected filters');actionPick.innerHTML='<div><div class="action-pick-title">TOP-RANKED OPPORTUNITY</div><div class="action-pick-symbol">No candidates for this filter</div><div class="action-pick-reason">No candidate matches the selected asset and stage filters.</div></div><span class="action-pick-status">NO MATCH</span>';}}const actionPickControl=document.getElementById('action-top-pick');if(actionPickControl){const openTopAnalysis=()=>{const symbol=actionPickControl.dataset.symbol,asset=actionPickControl.dataset.asset;if(!symbol||!asset)return;const card=[...document.querySelectorAll('.op-flippable')].find(c=>(c.dataset.symbol||'')===symbol&&(c.dataset.assetType||'').toLowerCase()===asset);if(!card)return;const library=document.getElementById('candidate-analysis-library');if(library)library.open=true;card.scrollIntoView({behavior:'smooth',block:'center'});if(!card.classList.contains('is-flipped'))card.click();};actionPickControl.addEventListener('click',openTopAnalysis);actionPickControl.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();openTopAnalysis();}});}const jAssetButtons=document.querySelectorAll('[data-jasset]'),jStageButtons=document.querySelectorAll('[data-jstage]');let jAsset='all',jStage='all';function applyActionFilters(){const cards=[...document.querySelectorAll('.op-flippable')];cards.forEach(card=>{const asset=(card.dataset.assetType||'').toLowerCase(),stage=(card.dataset.stage||'').toUpperCase();const assetOK=jAsset==='all'||asset===jAsset;let stageOK=true;if(jStage==='buy')stageOK=stage==='BUY';else if(jStage==='setup')stageOK=['ACTIONABLE SETUP','DEVELOPING SETUP','EARLY SETUP'].includes(stage);else if(jStage==='early')stageOK=['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);else if(jStage==='watch')stageOK=!['BUY','ACTIONABLE SETUP','EARLY SETUP','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);card.hidden=!(assetOK&&stageOK);});updateActionTopPick();const visible=cards.filter(c=>!c.hidden).length;const counts=document.getElementById('action-counts');if(counts)counts.innerHTML='<span><strong>'+visible+'</strong> shown / '+cards.length+' radar cards</span><span><strong>'+jAsset.toUpperCase()+'</strong> assets</span><span><strong>'+jStage.toUpperCase()+'</strong> stage</span>';}
jAssetButtons.forEach(btn=>btn.addEventListener('click',()=>{jAsset=btn.dataset.jasset;jAssetButtons.forEach(b=>b.classList.toggle('active',b===btn));applyActionFilters();}));jStageButtons.forEach(btn=>btn.addEventListener('click',()=>{jStage=btn.dataset.jstage;jStageButtons.forEach(b=>b.classList.toggle('active',b===btn));applyActionFilters();}));applyActionFilters();
document.querySelectorAll('.op-flippable').forEach(card=>{const flip=()=>{const on=card.classList.toggle('is-flipped');card.setAttribute('aria-pressed',String(on));};card.addEventListener('click',ev=>{if(ev.target.closest('details'))return;flip();});card.addEventListener('keydown',ev=>{if(ev.target.closest('details'))return;if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();flip();}});});
const timelineItems=surfaced.slice().sort((a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)).slice(0,12);document.getElementById('radar-timeline-cards').innerHTML=timelineItems.length?timelineItems.map(x=>{const history=(x.grade_history||[]).slice(-3).reverse();const events=history.length?history.map(h=>'<div class="timeline-event"><b>'+e(h.to||'Stage update')+'</b> · '+e(date(h.at))+'</div>').join(''):'<div class="timeline-event">First recorded stage: '+e(x.last_stage||'Watching')+'</div>';return '<article class="timeline-card"><div class="timeline-top"><div class="timeline-symbol">'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</div><span class="timeline-stage">'+e(x.last_stage||'WATCHING')+'</span></div><div class="timeline-events">'+events+'</div></article>';}).join(''):'<div class="op-empty">No candidates are currently on the radar.</div>';
const runnerPool=surfaced.filter(x=>x.asset_type==='stock').map(x=>{const gap=x.premarket_gap_pct==null?null:n(x.premarket_gap_pct),vol=n(x.premarket_volume),rvol=n(x.premarket_rvol_same_time),research=n(x.early_runner_research_score);const qualifies=(gap!==null&&gap>=2.5)||(vol>=25000&&rvol>=1.25);if(!qualifies)return null;const catalysts=Array.isArray(x.catalyst_labels)?x.catalyst_labels:(x.catalyst_labels?[String(x.catalyst_labels)]:[]);const dilution=Array.isArray(x.dilution_flags)?x.dilution_flags:(x.dilution_flags?[String(x.dilution_flags)]:[]);const score=Math.max(0,Math.min(100,Math.round(Math.min(Math.max(gap||0,0),25)*1.2+Math.min(rvol,10)*4+Math.min(vol/10000,10)+research*.25+(catalysts.length?12:0)-(dilution.length?15:0))));return {...x,_runnerScore:score,_runnerCatalysts:catalysts,_runnerDilution:dilution};}).filter(Boolean).sort((a,b)=>b._runnerScore-a._runnerScore).slice(0,40);
const runnerVerified=runnerPool.filter(x=>x.price_validation_status==='verified').length;const runnerLastKnown=runnerPool.filter(x=>x.price_validation_status==='last_known').length;
document.getElementById('early-runner-stats').innerHTML=[[''+runnerPool.length,'QUALIFIED RUNNERS'],[''+runnerVerified,'LIVE VERIFIED'],[''+runnerLastKnown,'LAST KNOWN · EOD'],[''+(runnerPool.length-runnerVerified-runnerLastKnown),'UNVERIFIED']].map(x=>'<div class="card"><div class="muted">'+e(x[1])+'</div><div class="value">'+e(x[0])+'</div></div>').join('');
document.getElementById('early-runner-rows').innerHTML=runnerPool.map((x,i)=>{const notes=[...x._runnerCatalysts,...x._runnerDilution.map(v=>'RISK: '+v)];const status=x.price_validation_status==='verified'?'<span class="pill pos">LIVE VERIFIED</span>':x.price_validation_status==='last_known'?'<span class="pill">LAST KNOWN · EOD</span>':'<span class="pill neg">UNVERIFIED</span>';const price=x.latest_price==null?'—':Number(x.latest_price).toPrecision(7);return '<tr><td>'+(i+1)+'</td><td><b>'+e(x.symbol)+'</b><div class="tiny">'+e(x.last_stage||'WATCHLIST')+'</div></td><td><b>'+e(x._runnerScore)+'/100</b><div class="tiny">Research '+e(x.early_runner_research_score??'—')+'</div></td><td>'+e(price)+'<div class="tiny">'+e(x.price_source||'Source not recorded')+'</div>'+priceTiming(x)+'</td><td>'+status+'</td><td>'+e(x.premarket_gap_pct==null?'—':pct(x.premarket_gap_pct))+'</td><td>'+e(x.premarket_rvol_same_time==null?'—':Number(x.premarket_rvol_same_time).toFixed(2)+'x')+'</td><td>'+e(x.premarket_volume==null?'—':Number(x.premarket_volume).toLocaleString())+'</td><td>'+e(notes.join(' · ')||'No catalyst / dilution flag recorded')+'</td></tr>';}).join('')||'<tr><td colspan="9" class="empty">No saved candidates currently meet the Early Runner display criteria. The board will populate from the next hourly scan that records qualifying premarket metrics.</td></tr>';
const observed=buySignals.filter(x=>x.target_1||x.target_2||x.stop_loss);const l1=observed.filter(x=>x.level_observations&&x.level_observations.target_1_observed),l2=observed.filter(x=>x.level_observations&&x.level_observations.target_2_observed),stops=observed.filter(x=>x.level_observations&&x.level_observations.stop_observed);document.getElementById('target-performance').innerHTML=[['BUY plans tracked',observed.length],['Level 1 observed',l1.length+' / '+observed.length],['Level 2 observed',l2.length+' / '+observed.length],['Stop observed',stops.length+' / '+observed.length]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');document.getElementById('target-tracking').innerHTML=observed.slice().sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,40).map(x=>{const obs=x.level_observations||{};return '<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e(x.entry_price??'—')+'</td><td>'+e(x.target_1??'—')+(obs.target_1_observed?'<div class="tiny pos">Observed '+e(date(obs.target_1_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.target_2??'—')+(obs.target_2_observed?'<div class="tiny pos">Observed '+e(date(obs.target_2_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.stop_loss??'—')+(obs.stop_observed?'<div class="tiny neg">Observed '+e(date(obs.stop_observed.at))+'</div>':'')+'</td><td>'+e(obs.target_1_observed?elapsed(x.at,obs.target_1_observed.at):'—')+'</td><td>'+e(obs.target_2_observed?elapsed(x.at,obs.target_2_observed.at):'—')+'</td></tr>';}).join('')||'<tr><td colspan="7" class="empty">BUY target observations will appear as Jarvis records new scans.</td></tr>';

const thresholds=[['Positive now',trackedReturns.filter(x=>n(x.discovery_return_pct)>0).length],['At least +5%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=5).length],['At least +10%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=10).length],['At least +20%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=20).length],['Down 5% or more',trackedReturns.filter(x=>n(x.discovery_return_pct)<=-5).length],['Ever reached +10%',trackedReturns.filter(x=>hasPct(x.discovery_max_gain_pct)&&n(x.discovery_max_gain_pct)>=10).length]];
document.getElementById('stats').innerHTML=[['Tracked candidates',all.length],['In latest scan',live.length],['Average return',avg===null?'—':pct(avg)],['Positive candidates',thresholds[0][1]+' / '+trackedReturns.length],['Lifetime checkpoint wins',Object.values(l.checkpoints||{}).reduce((s,x)=>s+n(x.wins),0)],['Top current return',trackedReturns.length?pct(Math.max(...trackedReturns.map(x=>n(x.discovery_return_pct)))):'—']].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');
document.getElementById('assets').innerHTML=['stock','crypto'].map(k=>{const a=trackedReturns.filter(x=>x.asset_type===k),m=a.length?a.reduce((s,x)=>s+n(x.discovery_return_pct),0)/a.length:null;return '<button type="button" class="panel asset-jump" data-asset="'+k+'" aria-label="Show '+(k==='stock'?'stock':'crypto')+' candidates"><div class="muted">'+(k==='stock'?'Stocks':'Crypto')+'</div><div class="value">'+a.length+' performance tracked</div><div class="'+cl(m)+'">Average return '+(m===null?'—':pct(m))+'</div><div class="tiny">'+a.filter(x=>n(x.discovery_return_pct)>0).length+' positive · '+a.filter(x=>x.last_stage==='ACTIONABLE SETUP').length+' actionable grade</div><div class="tiny" style="margin-top:8px;color:var(--accent)">View '+(k==='stock'?'stock':'crypto')+' list →</div></button>'}).join('');
document.getElementById('buy-history').innerHTML=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'<div class="tiny">'+e(x.status||'')+'</div></td><td>'+date(x.at)+'</td><td>'+e(x.entry_price)+'</td><td class="'+cl(x.latest_return_pct)+'">'+(x.latest_return_pct==null?'—':pct(x.latest_return_pct))+'</td>'+['1h','4h','1d','3d','5d','7d','10d'].map(h=>'<td class="'+cl(x.outcomes&&x.outcomes[h]&&x.outcomes[h].return_pct)+'">'+(x.outcomes&&x.outcomes[h]?pct(x.outcomes[h].return_pct):'—')+'</td>').join('')+'<td>'+e(x.score)+'/100</td></tr>').join('')||'<tr><td colspan="12" class="empty">No BUY recommendations have been recorded yet.</td></tr>';
const signalHorizonRows=['1h','4h','1d','3d','5d','7d','10d'].map(h=>{
 const values=buySignals.map(x=>x.outcomes&&x.outcomes[h]?Number(x.outcomes[h].return_pct):NaN).filter(Number.isFinite);
 const wins=values.filter(v=>v>0).length,losses=values.filter(v=>v<0).length;
 const avg=values.length?values.reduce((sum,v)=>sum+v,0)/values.length:null;
 return '<tr><td>'+h+'</td><td>'+values.length+'</td><td>'+wins+'</td><td>'+losses+'</td><td class="'+cl(avg)+'">'+(avg===null?'—':pct(avg))+'</td></tr>';
}).join('');
document.getElementById('signal-checks').innerHTML=signalHorizonRows||'<tr><td colspan="5" class="empty">No matured BUY signal outcomes yet.</td></tr>';

const byReturn=[...trackedReturns].sort((a,b)=>n(b.discovery_return_pct)-n(a.discovery_return_pct));document.getElementById('leaders').innerHTML=byReturn.slice(0,10).map((x,i)=>'<tr><td>'+(i+1)+'</td><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+n(x.discovery_initial_price??x.initial_price).toPrecision(7)+'</td><td>'+n(x.latest_price).toPrecision(7)+'</td><td class="'+cl(x.discovery_return_pct)+'">'+pct(x.discovery_return_pct)+'</td><td class="pos">'+pct(x.discovery_max_gain_pct??x.max_gain_pct)+'</td><td class="neg">'+pct(x.discovery_max_drawdown_pct??x.max_drawdown_pct)+'</td></tr>').join('')||'<tr><td colspan="7" class="empty">No tracked candidates yet.</td></tr>';
let ledgerAssetFilter='all';function rows(){const q=document.getElementById('q').value.toUpperCase(),a=ledgerAssetFilter,g=document.getElementById('gradeFilter').value;const stocks=all.filter(x=>x.asset_type==='stock').length,crypto=all.filter(x=>x.asset_type==='crypto').length;const v=all.filter(x=>(a==='all'||x.asset_type===a)&&(g==='all'||x.last_stage===g)&&(!q||String(x.symbol).toUpperCase().includes(q)));const countNode=document.getElementById('ledger-candidate-counts');if(countNode)countNode.innerHTML='<span><strong>'+all.length+'</strong> total</span><span><strong>'+stocks+'</strong> stocks</span><span><strong>'+crypto+'</strong> crypto</span><span><strong>'+v.length+'</strong> shown</span>';document.querySelectorAll('[data-ledger-asset]').forEach(btn=>{const active=btn.dataset.ledgerAsset===a;btn.classList.toggle('active',active);btn.setAttribute('aria-pressed',String(active));});v.sort((x,y)=>(rank[y.last_stage]||0)-(rank[x.last_stage]||0)||(y.last_seen||'').localeCompare(x.last_seen||''));document.getElementById('rows').innerHTML=v.map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.first_seen)+'</td><td>'+n(x.discovery_initial_price??x.initial_price).toPrecision(7)+'</td><td><span class="pill">'+e(x.last_stage||'—')+'</span></td><td>'+e(x.price_source||'Legacy record — source not recorded')+'<div class="tiny">'+e(x.price_freshness||'Freshness unknown')+(x.price_validation_status==='unverified'?' · PRICE UNVERIFIED — excluded from returns':'')+'</div></td><td>'+date(x.price_as_of||x.last_seen)+'</td><td class="'+cl(x.session_change_pct)+'">'+(x.price_validation_status==='unverified'||x.session_change_pct==null?'—':pct(x.session_change_pct))+'</td><td class="'+cl(x.discovery_return_pct)+'">'+(hasPct(x.discovery_return_pct)?pct(x.discovery_return_pct):(['first_verified_fallback','first_reliable_fallback'].includes(x.discovery_baseline_status)?'DELAYED RELIABLE BASELINE':(x.price_validation_status==='unverified'?'AWAITING RELIABLE PRICE':'—')))+'</td><td class="pos">'+(x.price_validation_status==='unverified'||!hasPct(x.discovery_max_gain_pct??x.max_gain_pct)?'—':pct(x.discovery_max_gain_pct??x.max_gain_pct))+'</td><td class="neg">'+(x.price_validation_status==='unverified'||!hasPct(x.discovery_max_drawdown_pct??x.max_drawdown_pct)?'—':pct(x.discovery_max_drawdown_pct??x.max_drawdown_pct))+'</td><td>'+n(x.age_days).toFixed(1)+'d</td></tr>').join('')||'<tr><td colspan="11" class="empty">No matches.</td></tr>'}
['q','gradeFilter'].forEach(id=>document.getElementById(id).addEventListener(id==='q'?'input':'change',rows));document.querySelectorAll('[data-ledger-asset]').forEach(btn=>btn.addEventListener('click',()=>{ledgerAssetFilter=btn.dataset.ledgerAsset;rows();}));document.querySelectorAll('.asset-jump').forEach(card=>card.addEventListener('click',()=>{ledgerAssetFilter=card.dataset.asset;document.getElementById('gradeFilter').value='all';document.getElementById('q').value='';rows();document.getElementById('candidate-ledger').scrollIntoView({behavior:'smooth',block:'start'});}));rows();
document.getElementById('thresholds').innerHTML=thresholds.map(x=>'<div style="display:flex;justify-content:space-between;padding:7px;border-bottom:1px solid #2b3b4d"><span>'+e(x[0])+'</span><b>'+x[1]+'</b></div>').join('');
const cps=l.checkpoints||{};document.getElementById('checks').innerHTML=Object.entries(cps).sort((a,b)=>n(a[0])-n(b[0])).map(([h,x])=>'<tr><td>'+e(h)+'</td><td>'+n(x.observations)+'</td><td>'+n(x.wins)+'</td><td>'+n(x.losses)+'</td><td>'+pct(x.observations?n(x.total_return_pct)/n(x.observations):0)+'</td></tr>').join('')||'<tr><td colspan="5">Checkpoints populate as records mature.</td></tr>';
const moves=[];all.forEach(x=>(x.grade_history||[]).forEach(m=>moves.push({...m,symbol:x.symbol,asset:x.asset_type})));moves.sort((a,b)=>new Date(b.at)-new Date(a.at));document.getElementById('moves').innerHTML=moves.slice(0,50).map(m=>'<tr><td>'+e((m.asset||'').toUpperCase()+' '+m.symbol)+'</td><td>'+e(m.from||'—')+'</td><td>'+e(m.to||'—')+'</td><td>'+date(m.at)+'</td></tr>').join('')||'<tr><td colspan="4">Grade history starts recording from this upgrade onward.</td></tr>';
document.getElementById('catalysts').innerHTML=all.filter(x=>(x.discovery_reasons||[]).length||(x.catalysts||[]).length||(x.risk_flags||[]).length).slice(0,40).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e((x.discovery_reasons||[]).join(' · ')||'—')+'</td><td>'+((x.catalysts||[]).slice(0,2).map(c=>c.url?'<a target="_blank" rel="noopener" href="'+e(c.url)+'">'+e(c.title)+'</a>':e(c.title)).join('<br>')||'—')+'</td><td class="neg">'+e((x.risk_flags||[]).join(' · ')||'—')+'</td></tr>').join('')||'<tr><td colspan="4">Discovery details will populate on the next scan.</td></tr>';
const faded=all.filter(x=>x.currently_surfaced===false).sort((a,b)=>(b.last_seen||'').localeCompare(a.last_seen||''));document.getElementById('faded').innerHTML=faded.slice(0,40).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.last_seen)+'</td><td class="'+cl(x.discovery_return_pct)+'">'+pct(x.discovery_return_pct)+'</td><td class="pos">'+pct(x.discovery_max_gain_pct??x.max_gain_pct)+'</td><td>'+e(x.last_stage||'—')+'</td></tr>').join('')||'<tr><td colspan="5">No candidates currently absent from the latest scan.</td></tr>';
}
"""


def analytics_tab_page(active: str) -> str:
    tabs = [
        ("discovery-performance", "Discovery Performance"),
        ("momentum-evidence", "Momentum Evidence"),
        ("market-breadth", "Market Breadth"),
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
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis · __TITLE__</title><style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#142235;--line:#2b4058;--muted:#9eb0c5;--text:#edf3fa;--green:#4bd6a0;--red:#ff7185;--amber:#f2c66d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:24px 18px 60px}
.eyebrow{font-size:10px;letter-spacing:.16em;color:#8ca8c5;font-weight:800}h1{font-size:clamp(26px,5vw,38px);margin:5px 0 4px}.muted,.sub{color:var(--muted)}.status{margin:12px 0 18px;color:var(--muted);font-size:12px}
.tabs{display:flex;gap:7px;flex-wrap:wrap;margin:18px 0 22px}.tabs a{padding:9px 13px;border:1px solid var(--line);border-radius:10px;color:var(--text);text-decoration:none;background:#101d2d}.tabs a.active{background:#203b55;border-color:#6d94ba;box-shadow:inset 0 0 0 1px #47617d}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:18px 0}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px}.card strong{display:block;font-size:25px;margin-top:5px}.card span{font-size:10px;color:var(--muted);letter-spacing:.1em}
.panel{margin-top:14px}.panel h2{font-size:16px;margin:0 0 5px}.scroll{overflow:auto;margin-top:12px}table{border-collapse:collapse;width:100%;min-width:680px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:10px;color:#9eb0c5;letter-spacing:.08em}td{font-size:12px}.pos{color:var(--green)}.neg{color:var(--red)}.neutral{color:var(--amber)}.empty{color:var(--muted);padding:18px}.foot{margin-top:18px;color:var(--muted);font-size:11px}.breadth-heatmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:8px;margin-top:14px}.breadth-heat-tile{border:1px solid #405568;border-radius:10px;min-height:72px;padding:9px;display:flex;flex-direction:column;justify-content:space-between;min-width:0;background:#1a2935}.breadth-heat-tile.positive{background:linear-gradient(145deg,#174b3d,#123329);border-color:#347b63}.breadth-heat-tile.negative{background:linear-gradient(145deg,#512c32,#342128);border-color:#87505a}.breadth-heat-tile.neutral{background:#273342}.breadth-heat-tile strong{font-size:13px;overflow-wrap:anywhere}.breadth-heat-tile b{font-size:16px}.breadth-heat-tile small{font-size:9px;color:#c4d0dc}@media(max-width:600px){.breadth-heatmap{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:600px){main{padding:18px 12px}.tabs a{font-size:12px;padding:8px 10px}}
</style></head><body><main><div class="eyebrow">JARVIS · MARKET INTELLIGENCE</div><h1>__TITLE__</h1><div class="sub">__SUBTITLE__</div><div class="status" id="status">Loading saved scan snapshot…</div>
<nav class="tabs">__NAV__<a href="/history">Overview</a><a href="/intelligence">Intelligence</a><a href="/paper-trading">Paper Trading</a><a href="/customize">Customize</a></nav>
<div id="summary" class="grid"><div class="card"><span>LOADING</span><strong>—</strong></div></div>
<section class="panel" id="breadthHeatmapPanel" style="display:__HEATMAP_DISPLAY__"><h2>Stock candidate heat map</h2><div class="muted">Quick view of surfaced stocks. Tile color reflects the latest recorded daily move when available, otherwise the saved return since discovery. This is a visual scan aid, not an entry signal.</div><div id="breadthHeatmap" class="breadth-heatmap"><div class="empty">Waiting for saved stock data…</div></div><div class="foot" id="breadthHeatmapFoot"></div></section>
<section class="panel"><h2>__TITLE__ detail</h2><div class="muted" id="description">All values are read from the latest saved scan; no additional market-data requests are triggered by this page.</div>
<div class="scroll"><table><thead id="thead"></thead><tbody id="tbody"><tr><td class="empty">Waiting for saved data…</td></tr></tbody></table></div></section>
<div class="foot">Research and observation only · Paper simulation only · No live orders. Missing or insufficient data is shown as unavailable rather than estimated.</div></main>
<script>
const kind="__KIND__";
const esc=v=>String(v??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
const fmt=v=>v==null||!Number.isFinite(Number(v))?"—":Number(v).toFixed(2)+"%";
const card=(v,l)=>'<div class="card"><span>'+esc(l)+'</span><strong>'+esc(v)+'</strong></div>';
const sign=v=>v==null?"":Number(v)>0?"pos":Number(v)<0?"neg":"neutral";
function render(d){
 const all=Object.values(d.paper_tracker||{}).filter(x=>x&&typeof x==="object");
 const surfaced=all.filter(x=>x.currently_surfaced!==false);
 const snapshot=d.last_scan_completed_at?new Date(d.last_scan_completed_at).toLocaleString():"timestamp unavailable";
 document.getElementById("status").textContent="Data snapshot: "+snapshot+" · "+surfaced.length+" surfaced · "+all.length+" tracked · "+(d.history_source||"source unknown");
 const summary=document.getElementById("summary"),head=document.getElementById("thead"),body=document.getElementById("tbody"),desc=document.getElementById("description");
 if(kind==="discovery-performance"){
  const valid=all.filter(x=>x.discovery_return_pct!=null&&Number.isFinite(Number(x.discovery_return_pct)));
  const winners=valid.filter(x=>Number(x.discovery_return_pct)>0);
  const avg=valid.length?valid.reduce((s,x)=>s+Number(x.discovery_return_pct),0)/valid.length:null;
  const best=[...valid].sort((a,b)=>Number(b.discovery_return_pct)-Number(a.discovery_return_pct))[0];
  summary.innerHTML=[card(valid.length,"MEASURED BASELINES"),card(winners.length+" / "+valid.length,"POSITIVE RETURNS"),card(fmt(avg),"AVERAGE RETURN"),card(best?best.symbol+" · "+fmt(best.discovery_return_pct):"—","TOP DISCOVERY")].join("");
  desc.textContent="Ranked by return from the saved discovery baseline. Provisional or unverified baselines remain identified in the status column.";
  head.innerHTML="<tr><th>ASSET</th><th>DISCOVERED</th><th>BASELINE</th><th>LATEST</th><th>RETURN</th><th>BEST</th><th>PRICE STATUS</th></tr>";
  const rows=[...all].filter(x=>x.discovery_return_pct!=null).sort((a,b)=>Number(b.discovery_return_pct)-Number(a.discovery_return_pct));
  body.innerHTML=rows.length?rows.slice(0,100).map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" · "+x.symbol)+'</b></td><td>'+esc(x.first_seen||"—")+'</td><td>'+esc(x.discovery_initial_price??"—")+'</td><td>'+esc(x.latest_price??"—")+'</td><td class="'+sign(x.discovery_return_pct)+'">'+fmt(x.discovery_return_pct)+'</td><td>'+fmt(x.discovery_max_gain_pct)+'</td><td>'+esc(x.price_validation_status||"unknown")+(x.discovery_baseline_status?" · "+esc(x.discovery_baseline_status):"")+'</td></tr>').join(""):'<tr><td colspan="7" class="empty">No measured discovery returns are available yet.</td></tr>';
 }else if(kind==="momentum-evidence"){
  const rows=surfaced.map(x=>{const c=x.score_components||x.early_runner_score_components||{};const fields=[x.daily_change_pct,c.momentum,c.volume,x.premarket_rvol_same_time,x.universe_rs_percentile,x.volume_acceleration_3bar,x.accumulation_signal_score,x.early_runner_research_score];const n=fields.filter(v=>v!=null&&Number.isFinite(Number(v))).length;return {...x,_n:n};}).filter(x=>x._n>0).sort((a,b)=>Number(b.universe_rs_percentile??-1)-Number(a.universe_rs_percentile??-1)||Number(b.daily_change_pct??-999)-Number(a.daily_change_pct??-999));
  summary.innerHTML=[card(rows.length,"WITH MOMENTUM DATA"),card(rows.filter(x=>Number(x.daily_change_pct)>0).length,"POSITIVE DAILY MOVE"),card(rows.filter(x=>Number(x.premarket_rvol_same_time)>=1.5).length,"PREMARKET RVOL ≥1.5x"),card(rows.filter(x=>x.multi_timeframe_alignment===true).length,"MULTI-TIMEFRAME ALIGNED"),card(rows.filter(x=>Number(x.accumulation_signal_score)>=50).length,"ACCUMULATION EVIDENCE"),card(rows.filter(x=>x.weekly_confirmation===true).length,"WEEKLY CONFIRMED")].join("");
  desc.textContent="Sorted by five-day stock-universe relative-strength percentile, then daily move. These are separate evidence fields, not a new signal score, and they do not change BUY eligibility.";
  head.innerHTML="<tr><th>ASSET</th><th>DAILY MOVE</th><th>PREMARKET RVOL</th><th>VOLUME ACCEL.</th><th>ACCUMULATION</th><th>LIQUIDITY SWEEP</th><th>MULTI-TF</th><th>UNIVERSE RS</th><th>WEEKLY TREND</th><th>WEEKLY BREAKOUT</th><th>BREAKOUT LEVEL</th><th>RUNNER RESEARCH</th></tr>";
  body.innerHTML=rows.length?rows.slice(0,100).map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" · "+x.symbol)+'</b></td><td class="'+sign(x.daily_change_pct)+'">'+fmt(x.daily_change_pct)+'</td><td>'+esc(x.premarket_rvol_same_time==null?"—":Number(x.premarket_rvol_same_time).toFixed(2)+"x")+'</td><td>'+esc(x.volume_acceleration_3bar==null?"—":Number(x.volume_acceleration_3bar).toFixed(2)+"x")+'</td><td>'+esc(x.accumulation_signal_score??"—")+'</td><td>'+esc(x.liquidity_sweep_detected===true?"Detected":x.liquidity_sweep_detected===false?"Not detected":"Unavailable")+'</td><td>'+esc(x.multi_timeframe_status||"Insufficient data")+'</td><td>'+esc(x.universe_rs_percentile==null?"—":x.universe_rs_percentile+"%")+'</td><td>'+esc(x.weekly_confirmation_status||"Insufficient history")+'</td><td>'+esc(x.weekly_breakout_confluence==null?"—":x.weekly_breakout_confluence?"Yes":"No")+'</td><td>'+esc(x.breakout_level_class||"Unavailable")+'</td><td>'+esc(x.early_runner_research_score??"—")+'</td></tr>').join(""):'<tr><td colspan="12" class="empty">Momentum fields will populate as completed scans save them.</td></tr>';
 }else{
  const stock=surfaced.filter(x=>x.asset_type==="stock");
  const breadth=stock.map(x=>x.market_breadth).find(x=>x&&typeof x==="object")||{};
  summary.innerHTML=[card(breadth.sample_size??"—","STOCK CANDIDATE SAMPLE"),card(fmt(breadth.above_20d_average_pct),"ABOVE 20-DAY AVERAGE"),card(fmt(breadth.positive_5d_pct),"POSITIVE 5-DAY RETURN"),card(fmt(breadth.breaking_20d_high_pct),"BREAKING 20-DAY HIGH"),card(breadth.regime||"Awaiting data","COHORT REGIME"),card(breadth.confidence||"—","SAMPLE CONFIDENCE")].join("");
  desc.textContent=breadth.scope||"Breadth data is not available yet. This metric will represent the currently returned stock candidates only, not all NYSE/Nasdaq listings.";
  head.innerHTML="<tr><th>METRIC</th><th>VALUE</th><th>INTERPRETATION</th></tr>";
  const metrics=[["Above 20-day average",breadth.above_20d_average_pct,"Share of scanned stocks trading above their 20-day average"],["Positive 5-day return",breadth.positive_5d_pct,"Share of scanned stocks with positive five-day return"],["Breaking 20-day high",breadth.breaking_20d_high_pct,"Share of scanned stocks at a 20-session breakout"],["Regime",breadth.regime,"Cohort-level context only"],["Confidence",breadth.confidence,"Based on available sample size"]];
  body.innerHTML=metrics.map(([label,value,note])=>'<tr><td><b>'+esc(label)+'</b></td><td>'+esc(value==null?"Unavailable":typeof value==="number"?value+"%":value)+'</td><td>'+esc(note)+'</td></tr>').join("");
  const heatNode=document.getElementById("breadthHeatmap");
  if(heatNode){
   const heatItems=[...stock].sort((a,b)=>{
    const value=x=>x.daily_change_pct!=null&&Number.isFinite(Number(x.daily_change_pct))?Number(x.daily_change_pct):x.discovery_return_pct!=null&&Number.isFinite(Number(x.discovery_return_pct))?Number(x.discovery_return_pct):null;
    return (value(b)??-Infinity)-(value(a)??-Infinity);
   }).slice(0,36);
   heatNode.innerHTML=heatItems.length?heatItems.map(x=>{
    const daily=x.daily_change_pct!=null&&Number.isFinite(Number(x.daily_change_pct))?Number(x.daily_change_pct):null;
    const ret=x.discovery_return_pct!=null&&Number.isFinite(Number(x.discovery_return_pct))?Number(x.discovery_return_pct):null;
    const value=daily===null?ret:daily,source=daily===null?(ret===null?"NO RETURN":"SINCE DISCOVERY"):"DAILY MOVE";
    const state=value===null?"neutral":value>0?"positive":value<0?"negative":"neutral";
    return '<div class="breadth-heat-tile '+state+'"><strong>'+esc(x.symbol||"—")+'</strong><b>'+(value===null?"—":fmt(value))+'</b><small>'+esc(source)+(x.price_validation_status==="unverified"?" · UNVERIFIED":"")+'</small></div>';
   }).join(""):'<div class="empty">No surfaced stock candidates to display.</div>';
   const foot=document.getElementById("breadthHeatmapFoot");
   if(foot)foot.textContent="Showing "+heatItems.length+" of "+stock.length+" surfaced stocks · verified "+stock.filter(x=>x.price_validation_status==="verified").length+" · last known "+stock.filter(x=>x.price_validation_status==="last_known").length+" · unverified "+stock.filter(x=>x.price_validation_status==="unverified").length+".";
  }
 }
}
fetch("/history/data",{cache:"no-store"}).then(r=>{if(!r.ok)throw Error("HTTP "+r.status);return r.json()}).then(render).catch(e=>{document.getElementById("status").textContent="Saved scan data unavailable · "+e.message;document.getElementById("tbody").innerHTML='<tr><td class="empty">Could not load saved history. Try again after the next successful sync.</td></tr>';});
</script></body></html>""";
    return (page.replace("__TITLE__", title)
                .replace("__SUBTITLE__", subtitle)
                .replace("__NAV__", nav)
                .replace("__KIND__", active)
                .replace("__HEATMAP_DISPLAY__", "block" if active == "market-breadth" else "none"))


def customize_page() -> str:
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis · Customize</title>
<style>:root{color-scheme:dark;--bg:#0b1220;--panel:#142235;--line:#2b4058;--muted:#9eb0c5;--text:#edf3fa;--accent:#4aa3df}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}main{max-width:980px;margin:auto;padding:22px 16px 55px}.tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin:18px 0 24px}.tabs a{padding:9px;border:1px solid var(--line);border-radius:10px;background:#101d2d;color:var(--text);text-decoration:none;text-align:center;font-size:12px}.tabs a.active{background:#203b55;border-color:#6d94ba}.panel{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:17px;margin:12px 0}.jarvis-compact .panel{padding:12px;margin:8px 0}.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px;color:var(--muted);font-size:12px;font-weight:700}.field select,.field input{width:100%;min-height:42px;border:1px solid var(--line);border-radius:9px;background:#0d1927;color:var(--text);padding:9px}.swatches{display:flex;gap:9px;flex-wrap:wrap}.swatch{width:35px;height:35px;border-radius:50%;border:2px solid #ffffff55;cursor:pointer}.swatch[aria-pressed="true"]{outline:2px solid white;outline-offset:3px}.preview{border:1px solid var(--line);border-radius:12px;padding:15px;background:#0d1927}.preview button{background:var(--accent);color:#07111c;border:0;border-radius:8px;padding:8px 12px;font-weight:800}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.actions button{border:1px solid var(--line);border-radius:9px;padding:10px 14px;background:#1a2d42;color:var(--text);font-weight:700}.note{color:var(--muted);font-size:11px;margin-top:12px}@media(max-width:600px){main{padding:17px 12px}}</style></head><body><main><div style="color:#8ca8c5;font-size:10px;letter-spacing:.16em;font-weight:800">JARVIS · PERSONAL WORKSPACE</div><h1 style="font-size:clamp(27px,5vw,38px);margin:6px 0">Customize</h1><div style="color:var(--muted)">Make the dashboard yours. These settings affect appearance only—not scans, rankings, signals, or trading rules.</div>
<nav class="tabs"><a href="/history">Overview</a><a href="/intelligence">Intelligence</a><a href="/paper-trading">Paper Trading</a><a href="/discovery-performance">Discovery Performance</a><a href="/momentum-evidence">Momentum Evidence</a><a href="/market-breadth">Market Breadth</a><a class="active" href="/customize" aria-current="page">Customize</a></nav>
<section class="panel"><h2 style="margin-top:0">Appearance</h2><div class="settings"><div class="field">Accent color<div class="swatches" id="swatches"><button class="swatch" style="background:#4aa3df" data-color="#4aa3df" aria-label="Blue"></button><button class="swatch" style="background:#9b7bea" data-color="#9b7bea" aria-label="Purple"></button><button class="swatch" style="background:#35c49a" data-color="#35c49a" aria-label="Green"></button><button class="swatch" style="background:#e08a55" data-color="#e08a55" aria-label="Orange"></button><button class="swatch" style="background:#e05d78" data-color="#e05d78" aria-label="Rose"></button><button class="swatch" style="background:#d2b65c" data-color="#d2b65c" aria-label="Gold"></button></div></div>
<label class="field">Font<select id="font"><option value="Inter,system-ui,sans-serif">Modern · Inter / System</option><option value="Arial,Helvetica,sans-serif">Clean · Arial</option><option value="Georgia,serif">Editorial · Georgia</option><option value="ui-monospace,SFMono-Regular,monospace">Technical · Monospace</option></select></label>
<label class="field">Font size<select id="fontSize"><option value="13">Small</option><option value="14">Default</option><option value="15">Large</option><option value="16">Extra large</option></select></label>
<label class="field">Layout density<select id="density"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label></div>
<div class="actions"><button id="reset" type="button">Reset to defaults</button></div><div class="note">Preferences are saved in this browser on this device and automatically apply across Jarvis dashboard pages.</div></section>
<section class="panel"><h2 style="margin-top:0">Live preview</h2><div class="preview"><div style="font-weight:800;letter-spacing:.1em">JARVIS · MARKET INTELLIGENCE</div><p>Early-move detection · stocks + digital assets</p><button type="button">Sample action button</button></div></section>
</main><script>
const KEY='jarvisDashboardPrefsV1',defaults={radarFilter:'all',assetFilter:'all',density:'comfortable',showTimeline:true,showPerformance:true,accent:'#4aa3df',font:'Inter,system-ui,sans-serif',fontSize:'14'};
let prefs={...defaults};try{prefs={...prefs,...JSON.parse(localStorage.getItem(KEY)||'{}')}}catch(_){}
const $=id=>document.getElementById(id);
function paint(){document.documentElement.style.setProperty('--accent',prefs.accent);document.body.style.fontFamily=prefs.font;document.body.style.fontSize=(Number(prefs.fontSize)||14)+'px';document.body.classList.toggle('jarvis-compact',prefs.density==='compact');$('font').value=prefs.font;$('fontSize').value=prefs.fontSize;$('density').value=prefs.density;document.querySelectorAll('[data-color]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.color===prefs.accent)));}
function save(){try{localStorage.setItem(KEY,JSON.stringify(prefs))}catch(_){}paint();}
document.querySelectorAll('[data-color]').forEach(b=>b.addEventListener('click',()=>{prefs.accent=b.dataset.color;save()}));$('font').addEventListener('change',e=>{prefs.font=e.target.value;save()});$('fontSize').addEventListener('change',e=>{prefs.fontSize=e.target.value;save()});$('density').addEventListener('change',e=>{prefs.density=e.target.value;save()});$('reset').addEventListener('click',()=>{prefs={...defaults};save()});paint();
</script></body></html>"""


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
        html = response.body.decode("utf-8")
        if "</body>" in html and "jarvis-workspace-preferences" not in html:
            response.body = html.replace("</body>", injection + "</body>", 1).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
    except (AttributeError, UnicodeDecodeError):
        return response
    return response

@app.get("/customize", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def customize():
    return HTMLResponse(customize_page(), headers={"Cache-Control":"no-store"})
    
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
    return Response(content=HISTORY_DASHBOARD_JS, media_type="application/javascript",
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
        "discovery_baseline_status",
        "discovery_baseline_captured_at",
        "discovery_baseline_source",
    )
    history_fields = ("discovery_return_pct", "discovery_max_gain_pct", "discovery_max_drawdown_pct")
    captured_at = now_iso()
    for key, record in paper.items():
        if not isinstance(record, dict):
            continue

        # A previously established baseline wins over a later/missing upstream value.
        old = previous.get(key) if isinstance(previous, dict) else None
        if isinstance(old, dict):
            old_price = safe_float(old.get("discovery_initial_price"), 0.0)
            current_price = safe_float(record.get("discovery_initial_price"), 0.0)
            if old_price > 0 and (current_price <= 0 or not np.isclose(current_price, old_price, rtol=1e-12, atol=1e-12)):
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


@app.post("/history/sync")
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


@app.get("/paper-trading", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def paper_trading_page():
    return apply_workspace_preferences(HTMLResponse("""<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis Paper Trading</title><style>
:root{color-scheme:dark;--bg:#0b1119;--surface:#121e2b;--surface2:#172638;--line:#293b50;--muted:#9aabbd;--text:#edf3fa;--accent:#83b8ff;--green:#63d9a0;--red:#ff9292;--amber:#f0c477}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1a2b40 0%,var(--bg) 58%);color:var(--text);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{width:min(100% - 32px,1320px);margin:auto;padding:28px 0 55px}a{color:var(--accent);text-decoration:none}.eyebrow{font-size:10px;letter-spacing:.16em;color:#8edbd5;font-weight:800}h1{font-size:clamp(28px,4vw,38px);margin:5px 0}.muted{color:var(--muted);font-size:12px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0;border-bottom:1px solid var(--line);padding-bottom:10px}.tabs a{padding:8px 14px;border:1px solid var(--line);border-radius:10px;color:var(--muted)}.tabs .active{background:#16392f;color:#9ae5c3;border-color:#397e69}.hero,.metric,.panel{background:linear-gradient(145deg,var(--surface2),var(--surface));border:1px solid var(--line);border-radius:14px;padding:16px}.hero{display:grid;grid-template-columns:1.5fr 1fr;gap:18px}.notice{border-left:3px solid var(--green);padding:11px;background:#63d9a012;border-radius:7px;color:#c6e9dc;font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;margin:15px 0 25px}.metric .k{font-size:10px;color:var(--muted);letter-spacing:.08em}.metric strong{display:block;font-size:25px;margin-top:5px;font-variant-numeric:tabular-nums}.section{margin-top:26px}.scroll{overflow:auto;border:1px solid var(--line);border-radius:12px;margin-top:12px}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #26384a;font-size:12px;vertical-align:top}th{background:#172638;color:#b9c9da;font-size:10px;text-transform:uppercase;letter-spacing:.06em}tbody tr:nth-child(even){background:#ffffff06}.empty{text-align:center;padding:20px;color:var(--muted)}.pos{color:var(--green)}.neg{color:var(--red)}.pill{padding:3px 7px;border-radius:999px;background:#293b50;font-size:10px}.foot{font-size:11px;color:var(--muted);margin-top:22px}.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:9px}.rule{padding:12px;border:1px solid var(--line);border-radius:10px;background:#101b28;font-size:11px;color:#c1d0df}.rule b{display:block;color:#e6f3fb;margin-bottom:4px}
@media(max-width:700px){main{width:calc(100% - 24px);padding-top:20px}.tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;border-bottom:0;padding-bottom:0}.tabs a{display:flex;align-items:center;justify-content:center;text-align:center;white-space:normal;overflow-wrap:anywhere;min-height:40px;padding:8px 6px;font-size:12px;line-height:1.25}.hero{grid-template-columns:1fr}.metric{padding:12px}.metric strong{font-size:21px}}
</style></head><body><main><div class="eyebrow">JARVIS · SIMULATED EXECUTION</div><h1>Paper Trading</h1><div class="muted" id="updated">Loading paper account…</div>
<nav class="tabs"><a href="/history">Overview</a><a href="/intelligence">Intelligence</a><a class="active" href="/paper-trading" aria-current="page">Paper Trading</a><a href="/discovery-performance">Discovery Performance</a><a href="/momentum-evidence">Momentum Evidence</a><a href="/market-breadth">Market Breadth</a><a href="/customize">Customize</a></nav>
<section class="hero"><div><div class="eyebrow">AUTOMATIC BUY-SIGNAL PAPER EXECUTION</div><h2 style="margin:6px 0">A $250 virtual account. Real signals. Simulated fills.</h2><div class="muted">Jarvis automatically opens simulated positions when an official BUY signal has a verified price inside its saved entry range. It manages stop and target observations on subsequent completed scans.</div></div><div class="notice"><b>PAPER ONLY · NO BROKER CONNECTION</b><br>This account is isolated from Coinbase and Webull. No real orders, deposits, withdrawals, or account balances are accessed. Prices are sampled during scans; simulated fills are not guaranteed market executions.</div></section>
<div class="grid" id="metrics"><div class="metric"><div class="k">STARTING BALANCE</div><strong>$250.00</strong></div></div>
<section class="section"><h2>Open positions</h2><div class="muted">Live-verified scan prices only. Positions remain open until a saved stop or target is observed.</div><div class="scroll"><table><thead><tr><th>Asset</th><th>Entry</th><th>Latest</th><th>Quantity</th><th>Market value</th><th>Stop</th><th>Target 1</th><th>Target 2</th><th>Unrealized P/L</th><th>Opened</th></tr></thead><tbody id="positions"><tr><td colspan="10" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="section"><h2>Closed trades</h2><div class="scroll"><table><thead><tr><th>Asset</th><th>Entry</th><th>Exit</th><th>Quantity</th><th>Reason</th><th>Realized P/L</th><th>Closed</th></tr></thead><tbody id="closed"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="section"><h2>Execution ledger</h2><div class="muted">Every simulated BUY, partial/full SELL, and skipped signal is recorded with its reason.</div><div class="scroll"><table><thead><tr><th>Time</th><th>Action</th><th>Details</th><th>Price</th><th>Quantity</th><th>Fee</th><th>Signal</th></tr></thead><tbody id="ledger"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="section"><h2>Risk and execution rules</h2><div class="rules"><div class="rule"><b>Entry eligibility</b>Official BUY signal, independently verified price, and price inside saved entry range. No unverified or watch-only entries.</div><div class="rule"><b>Position sizing</b>Targets 1% of account equity at risk per trade, capped at 25% of equity per position and four open positions.</div><div class="rule"><b>Exit handling</b>Sell 50% when target 1 is first observed, the rest at target 2, or the full remaining position at the stop.</div><div class="rule"><b>Costs</b>Coinbase One Basic regular-app trades use $0 eligible trading commission within $500 monthly volume; the simulator estimates a 0.50% spread per side and tracks monthly volume. Standard fees after the allowance are not yet configured, so returns beyond the cap may be overstated. Webull U.S. stocks use $0 commission plus modeled CAT/SEC pass-through fees.</div></div></section>
<div class="foot">This is a scan-based paper simulation, not a tick-by-tick backtest. If a scan misses a price crossing between scans, the ledger records only what the next verified observation shows. Open-position P/L deducts entry fees and estimated exit fees; actual spreads and fees can differ, and Coinbase standard fees after the monthly allowance are not configured. Starting capital is fixed at $250.00.</div></main>
<script>
const esc=v=>String(v??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#39;");
const money=v=>Number.isFinite(Number(v))?"$"+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4}):"—";
const qty=v=>Number.isFinite(Number(v))?Number(v).toLocaleString(undefined,{maximumFractionDigits:8}):"—";
const stamp=v=>v?new Date(v).toLocaleString():"—";
function render(d){
 const a=d.account||{},s=d.summary||{};
 document.getElementById("updated").textContent="Last paper-account update · "+stamp(a.last_updated_at)+" · "+(a.mode||"PAPER_ONLY");
 const metrics=[["Starting balance",money(s.starting_cash)],["Account equity",money(s.equity)],["Available cash",money(s.cash)],["Open market value",money(s.market_value)],["Total return",(Number(s.total_return_pct)||0).toFixed(2)+"%"],["Realized P/L",money(s.realized_pnl)],["Unrealized P/L (net est. fees)",money(s.unrealized_pnl)],["Estimated exit fees",money(s.estimated_exit_fees)],["Coinbase One monthly volume",money(s.coinbase_one_monthly_volume)],["Allowance remaining",money(s.coinbase_one_remaining_allowance)],["Open positions",s.open_positions||0],["Closed trades",s.closed_trades||0]];
 document.getElementById("metrics").innerHTML=metrics.map(x=>'<div class="metric"><div class="k">'+esc(x[0])+'</div><strong>'+esc(x[1])+'</strong></div>').join("");
 const positions=a.positions||[];
 document.getElementById("positions").innerHTML=positions.length?positions.map(p=>{const pnl=(Number(p.latest_price||p.entry_price)-Number(p.entry_price))*Number(p.quantity)-Number(p.entry_fee_remaining||0)-Number(p.estimated_exit_fee||0);return '<tr><td><b>'+esc((p.asset_type||"").toUpperCase()+" "+p.symbol)+'</b><div class="pill">'+esc(p.score_model||"BUY signal")+'</div></td><td>'+money(p.entry_price)+'</td><td>'+money(p.latest_price)+'</td><td>'+qty(p.quantity)+'</td><td>'+money(Number(p.quantity)*Number(p.latest_price||p.entry_price))+'</td><td>'+money(p.stop_loss)+'</td><td>'+money(p.target_1)+(p.target_1_filled?' · 50% sold':'')+'</td><td>'+money(p.target_2)+'</td><td class="'+(pnl>=0?'pos':'neg')+'">'+money(pnl)+'</td><td>'+stamp(p.opened_at)+'</td></tr>';}).join(""):'<tr><td colspan="10" class="empty">No open paper positions yet. Jarvis will enter only when a verified BUY signal is inside its entry range.</td></tr>';
 const closed=a.closed_trades||[];
 document.getElementById("closed").innerHTML=closed.length?closed.slice().reverse().map(p=>'<tr><td>'+esc((p.asset_type||"").toUpperCase()+" "+p.symbol)+'</td><td>'+money(p.entry_price)+'</td><td>'+money(p.close_price)+'</td><td>'+qty(p.initial_quantity)+'</td><td>'+esc(p.close_reason||"—")+'</td><td class="'+(Number(p.realized_pnl)>=0?'pos':'neg')+'">'+money(p.realized_pnl)+'</td><td>'+stamp(p.closed_at)+'</td></tr>').join(""):'<tr><td colspan="7" class="empty">No closed trades yet.</td></tr>';
 const ledger=a.ledger||[];
 document.getElementById("ledger").innerHTML=ledger.length?ledger.slice().reverse().map(x=>'<tr><td>'+stamp(x.at)+'</td><td><span class="pill">'+esc(x.type)+'</span></td><td>'+esc(x.message)+'</td><td>'+money(x.price)+'</td><td>'+qty(x.quantity)+'</td><td>'+money(x.fee)+'</td><td>'+esc(x.signal_id||"—")+'</td></tr>').join(""):'<tr><td colspan="7" class="empty">No paper execution events recorded yet.</td></tr>';
}
fetch("/paper-trading/data?refresh="+Date.now(),{cache:"no-store"}).then(r=>{if(!r.ok)throw Error("HTTP "+r.status);return r.json()}).then(render).catch(e=>{document.getElementById("updated").textContent="Paper account data unavailable · "+e.message;});
</script></body></html>"""))


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
        item["premarket_quote"] = {
            "price": item.get("intraday_price") if item.get("intraday_price") else None,
            "as_of": item.get("intraday_as_of"),
            "source": "Yahoo Finance extended-hours intraday bars",
            "fresh": bool(item.get("intraday_quote_available")),
            "verification": item["premarket_alert_status"],
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
    return {
        "scanner": "Jarvis Premarket Early Runner",
        "version": "1.0",
        "timestamp": now_iso(),
        "market_session": session,
        "symbols_requested": len(universe),
        "candidates_found": len(ranked),
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
    provider_activity = scan_metrics_snapshot()
    alpaca_status_counts = {}
    for item in stock_results:
        status = item.get("alpaca_quote_status", "not_reported")
        alpaca_status_counts[status] = alpaca_status_counts.get(status, 0) + 1
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
                    "methods": {method: sum(1 for x in crypto_results if x.get("price_validation_method") == method) for method in ("kraken_latest_trade_cross_check", "coinbase_ticker_timestamp")},
                },
            },
            "alpaca_quote_status": {
                "credentials_configured": bool(os.getenv("ALPACA_API_KEY", "").strip() and os.getenv("ALPACA_API_SECRET", "").strip()),
                "feed": os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower() or "iex",
                "candidate_status_counts": alpaca_status_counts,
                "provider_activity": provider_activity.get("providers", {}).get("alpaca_snapshot_batch", {"calls": 0, "failures": 0, "total_seconds": 0.0, "max_seconds": 0.0, "error_counts": {}}),
            },
            "provider_note": "Counts show requested symbols and scan outcomes. A symbol not returned may have insufficient history, invalid market data, or a provider error; low scores alone no longer remove otherwise valid discoveries.",
            "webull_quote_status": "enabled" if WEBULL_QUOTE_ENABLED else "disabled_pending_authorization",
            "webull_quote_note": "Webull quote lookups are opt-in because app authorization was observed pending; Yahoo/Stooq scanning continues independently. Enable only after Webull market-data authorization is confirmed.",
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
    return apply_workspace_preferences(HTMLResponse("""<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis Intelligence</title><style>
:root{color-scheme:dark;--bg:#0b1119;--surface:#121e2b;--surface2:#172638;--line:#293b50;--muted:#9aabbd;--text:#edf3fa;--accent:#83b8ff;--green:#63d9a0;--amber:#f0c477}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1a2b40 0%,var(--bg) 58%);color:var(--text);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{width:min(100% - 32px,1320px);margin:auto;padding:28px 0 55px}
a{color:var(--accent);text-decoration:none}.muted,.sub{color:var(--muted);font-size:13px}.eyebrow{font-size:10px;letter-spacing:.15em;color:var(--accent);font-weight:750}h1{font-size:clamp(28px,4vw,38px);margin:5px 0}h2{font-size:19px;margin:0 0 6px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0;border-bottom:1px solid var(--line);padding-bottom:10px;max-width:100%}.tabs a{padding:8px 12px;border:1px solid var(--line);border-radius:10px;color:var(--muted);white-space:nowrap;min-width:0}.tabs .active{background:#20344a;color:var(--text);border-color:#47617d}
.hero,.metric,.feature{background:linear-gradient(145deg,var(--surface2),var(--surface));border:1px solid var(--line);border-radius:14px;padding:16px}.hero{display:grid;grid-template-columns:1.6fr 1fr;gap:18px}.hero p{color:var(--muted);margin:8px 0 0}.notice{border-left:3px solid var(--amber);padding:10px;background:#f0c47712;border-radius:7px;color:#e8d7b4;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin:15px 0 25px}.metric .k{font-size:10px;color:var(--muted);letter-spacing:.08em}.metric strong{display:block;font-size:25px;margin-top:5px}
.section{margin-top:26px}.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:10px;margin-top:12px}.feature b{font-size:13px}.feature p{font-size:12px;color:var(--muted);margin:5px 0}.status{font-size:10px;color:var(--amber);text-transform:uppercase;letter-spacing:.08em}.status.partial{color:var(--green)}
.scroll{overflow:auto;border:1px solid var(--line);border-radius:12px;margin-top:12px}table{width:100%;border-collapse:collapse;min-width:780px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #26384a;font-size:12px;vertical-align:top}th{background:#172638;color:#b9c9da;font-size:10px;text-transform:uppercase;letter-spacing:.06em}tbody tr:nth-child(even){background:#ffffff06}.pill{padding:3px 7px;border-radius:999px;background:#293b50;font-size:10px}.empty{text-align:center;padding:20px;color:var(--muted)}.foot{font-size:11px;color:var(--muted);margin-top:22px}
@media(max-width:700px){main{width:calc(100% - 24px);padding-top:20px}.hero{grid-template-columns:1fr}.metric{padding:12px}.metric strong{font-size:21px}}
</style></head><body><main><div class="eyebrow">JARVIS · MARKET INTELLIGENCE</div><h1>Intelligence</h1><div class="sub" id="updated">Loading saved research records…</div>
<nav class="tabs"><a href="/history">Overview</a><a class="active" href="/intelligence" aria-current="page">Intelligence</a><a href="/paper-trading">Paper Trading</a><a href="/discovery-performance">Discovery Performance</a><a href="/momentum-evidence">Momentum Evidence</a><a href="/market-breadth">Market Breadth</a><a href="/customize">Customize</a></nav>
<section class="hero"><div><div class="eyebrow">UNIFIED BUY INTELLIGENCE · OFFLINE PILOT</div><h2>One model. One evidence loop. Continuously improving.</h2><p>This is Jarvis’s single intelligence workspace for early detection, BUY-signal evidence, and measured outcomes. The model can inform offline BUY ranking; strict verification and risk gates remain in force. No live orders are placed.</p></div><div class="notice"><b>Offline validation mode</b><br>Existing data is shown separately from measurements that still need to be added. Missing values stay blank rather than being guessed.<div id="sessionStatus" style="margin-top:9px;color:#c5d6e8">Checking market session and quote providers…</div></div></section>
<div class="grid" id="stats"></div>
<section class="section"><h2>Market data quality</h2><div class="muted">Selected-price data types for currently surfaced candidates. Historical daily/hourly bars are shown separately from the price used as the current reference.</div><div class="features" id="dataQualityStats"><div class="feature">Loading data-quality counts…</div></div><div class="muted" id="dataQualityNote" style="margin-top:10px"></div></section>
<section class="section"><h2>Professional signal checklist</h2><div class="muted">A transparent status for each proposed research feature.</div><div class="features" id="features"></div></section>
<section class="section"><h2>Signal outcome tracking</h2><div class="muted">Forward returns are the first completed scan at or after each horizon. Excursions and breakout failures use sampled scan observations, not tick-level or guaranteed execution data.</div><div class="grid" id="researchOutcomes"><div class="metric"><div class="k">Awaiting data</div><strong>—</strong></div></div></section>
<section class="section"><h2>BUY opportunities &amp; Intelligence discoveries</h2><div class="muted">Potential plans are hypothetical reference levels from Jarvis. Stops and targets are not guaranteed fills. Existing BUY engine signals are kept separate from experimental research matches.</div>
<div style="margin-top:16px"><h3 style="font-size:14px;margin:0 0 5px">Current Jarvis BUY signals</h3><div class="muted">Official scanner BUY-grade records with their saved entry, stop and target levels.</div><div class="scroll"><table style="min-width:900px"><thead><tr><th>Candidate</th><th>Origin</th><th>Scoring model</th><th>Trigger / reason</th><th>Entry range</th><th>Stop loss</th><th>Target 1</th><th>Target 2</th><th>R:R</th><th>Status</th></tr></thead><tbody id="buyLedger"><tr><td colspan="10" class="empty">Loading BUY records…</td></tr></tbody></table></div></div>
<div style="margin-top:18px"><h3 style="font-size:14px;margin:0 0 5px">Intelligence matches</h3><div class="muted">Candidates flagged by the unified early-warning model, shown separately from official BUY-grade signals.</div><div class="scroll"><table style="min-width:900px"><thead><tr><th>Candidate</th><th>Model evidence</th><th>Jarvis grade</th><th>Entry range</th><th>Stop loss</th><th>Target 1</th><th>Target 2</th><th>Intelligence status</th></tr></thead><tbody id="researchBuyLedger"><tr><td colspan="8" class="empty">Loading research matches…</td></tr></tbody></table></div></div>
</section>
<section class="section"><h2>BUY readiness watch</h2><div class="muted">Candidates closest to the existing BUY requirements, showing confirmed gates and specific blockers. Informational only; BUY rules are unchanged.</div><div class="scroll"><table style="min-width:900px"><thead><tr><th>Candidate</th><th>Readiness</th><th>Price verification</th><th>Passed conditions</th><th>Still missing</th></tr></thead><tbody id="buyReadinessLedger"><tr><td colspan="5" class="empty">Loading readiness records…</td></tr></tbody></table></div></section>
<section class="section"><h2>Candidate research ledger</h2><div class="muted">Latest saved records; metrics appear only after the scanner actually records them.</div><div class="scroll"><table><thead><tr><th>Asset</th><th>Grade</th><th>Price validation</th><th>Relative strength</th><th>Same-time RVOL</th><th>VWAP / opening range</th><th>ADX / DMI</th><th>Liquidity / spread</th><th>Catalyst / dilution</th></tr></thead><tbody id="ledger"><tr><td colspan="9" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="section"><h2>How we will judge the signals</h2><div class="features">
<div class="feature"><b>Forward returns</b><p>1h, 4h, 24h, 72h, 5-day and 10-day observations where verified timestamps and prices exist.</p><span class="status partial">Sampling enabled · waiting for completed scan horizons</span></div>
<div class="feature"><b>Favorable / adverse excursion</b><p>Measure the best upside and worst pullback after each signal, not only the final return.</p><span class="status partial">Sampling enabled · waiting for completed scan horizons</span></div>
<div class="feature"><b>False-breakout rate</b><p>Record failed breaks, retests, spread and slippage assumptions.</p><span class="status partial">Sampling enabled · waiting for completed scan horizons</span></div>
<div class="feature"><b>Promotion gate</b><p>No research signal enters BUY scoring until enough verified observations show an improvement after costs.</p><span class="status partial">BUY rules remain unchanged</span></div>
</div></section>
<div class="foot">All research is observational and hypothetical. Quotes may be delayed or incomplete. Nothing here represents an executed trade or guaranteed fill.</div></main>
<script>
const esc=v=>String(v??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#39;");
const pick=(o,ks)=>{for(const k of ks)if(o&&o[k]!==undefined&&o[k]!==null&&o[k]!=="")return esc(o[k]);return "—"};
const researchDetail=(value)=>{
 if(value===undefined||value===null||value==="")return "—";
 if(Array.isArray(value)){const items=value.map(researchDetail).filter(x=>x!=="—");return items.length?items.join('<div class="research-detail-sep"></div>'):"—";}
 if(typeof value==="object"){
  const preferred=["name","title","catalyst","event","type","status","risk_level","risk","level","assessment","description","details","note","date","filing","source","reason"];
  const keys=preferred.filter(k=>value[k]!==undefined&&value[k]!==null&&value[k]!=="");
  const selected=keys.length?keys:Object.keys(value).filter(k=>value[k]!==undefined&&value[k]!==null&&typeof value[k]!=="object");
  const parts=selected.map(k=>'<div><span class="muted">'+esc(k.replaceAll("_"," "))+"</span>: "+researchDetail(value[k])+"</div>");
  if(value.url)parts.push('<div><a target="_blank" rel="noopener" href="'+esc(value.url)+'">Source</a></div>');
  return parts.length?'<div class="research-detail">'+parts.join("")+"</div>":"Details recorded";
 }
 return esc(String(value));
};
function render(d){const all=Object.values(d.paper_tracker||{}),verified=all.filter(x=>x.price_validation_status!=="unverified");
document.getElementById("updated").textContent="Saved snapshot · "+(d.synced_at?new Date(d.synced_at).toLocaleString():"sync timestamp unavailable");
const session=d.market_session||{}, providers=d.quote_provider_status||{};
document.getElementById("sessionStatus").textContent="US equities: "+(session.phase||"session unknown")+" · "+(providers.massive_live_quotes||"Massive live quotes: status unknown")+" · "+(providers.webull||"Webull: status unknown")+" · "+(session.calendar_note||"");
const stats=[["Tracked candidates",all.length],["Stocks",all.filter(x=>x.asset_type==="stock").length],["Crypto",all.filter(x=>x.asset_type==="crypto").length],["Price verified",verified.length],["Stocks price verified",all.filter(x=>x.asset_type==="stock"&&x.price_validation_status==="verified").length],["Crypto price verified",all.filter(x=>x.asset_type==="crypto"&&x.price_validation_status==="verified").length],["Crypto price unverified",all.filter(x=>x.asset_type==="crypto"&&x.price_validation_status==="unverified").length],["Currently surfaced",all.filter(x=>x.currently_surfaced!==false).length]];
document.getElementById("stats").innerHTML=stats.map(x=>'<div class="metric"><div class="k">'+esc(x[0])+'</div><strong>'+esc(x[1])+'</strong></div>').join("");
 const current=all.filter(x=>x.currently_surfaced!==false);
 const qualityType=x=>(x.data_quality&&x.data_quality.data_type)||"unknown";
 const qualityCards=["stock","crypto"].flatMap(asset=>["live","delayed","eod","historical","unknown"].map(type=>[asset.toUpperCase()+" · "+type.toUpperCase(),current.filter(x=>x.asset_type===asset&&qualityType(x)===type).length]));
 const eligible={stock:current.filter(x=>x.asset_type==="stock"&&x.data_quality&&x.data_quality.buy_eligible===true).length,crypto:current.filter(x=>x.asset_type==="crypto"&&x.data_quality&&x.data_quality.buy_eligible===true).length};
 qualityCards.push(["LIVE BUY-ELIGIBLE · STOCK",eligible.stock],["LIVE BUY-ELIGIBLE · CRYPTO",eligible.crypto]);
 qualityCards.push(
  ["STOCK DAILY · EOD",current.filter(x=>x.asset_type==="stock"&&x.daily_data_quality&&x.daily_data_quality.data_type==="eod").length],
  ["STOCK DAILY · HISTORICAL",current.filter(x=>x.asset_type==="stock"&&x.daily_data_quality&&x.daily_data_quality.data_type==="historical").length],
  ["STOCK INTRADAY · UNKNOWN",current.filter(x=>x.asset_type==="stock"&&x.intraday_data_quality&&x.intraday_data_quality.data_type==="unknown").length],
  ["CRYPTO HOURLY · HISTORICAL",current.filter(x=>x.asset_type==="crypto"&&x.hourly_data_quality&&x.hourly_data_quality.data_type==="historical").length],
  ["CRYPTO HOURLY · UNKNOWN",current.filter(x=>x.asset_type==="crypto"&&x.hourly_data_quality&&x.hourly_data_quality.data_type==="unknown").length]
 );
 document.getElementById("dataQualityStats").innerHTML=qualityCards.map(x=>'<div class="feature"><b>'+esc(x[0])+'</b><p style="font-size:23px;margin:4px 0">'+esc(x[1])+'</p></div>').join("");
 document.getElementById("dataQualityNote").textContent="Live BUY-eligible prices: "+eligible.stock+" stocks and "+eligible.crypto+" crypto. Delayed, EOD, historical, and unknown prices are research-only. Yahoo intraday bar delay entitlement is not confirmed and is not labelled live.";

const outcomes=d.research_outcomes_summary||{}, forward=outcomes.forward_returns||{};
const outcomeCards=[["Research tracked",outcomes.tracked||0],["Average favorable excursion",(outcomes.mfe_average_pct??0).toFixed(2)+"%"],["Average adverse excursion",(outcomes.mae_average_pct??0).toFixed(2)+"%"],["False breakouts",(outcomes.false_breakouts||0)+" / "+(outcomes.breakout_events||0)]];
for(const h of ["1h","4h","24h","72h","5d","10d"])outcomeCards.push([h+" forward return",(forward[h]?.average_return_pct??"—")+"% · "+(forward[h]?.observations||0)+" obs"]);
const cohorts=d.early_runner_cohorts||{};
for(const [name,c] of Object.entries(cohorts))outcomeCards.push([name,c.observations+" obs · "+(c.runner_rate_pct===null?"—":c.runner_rate_pct+"% runners")]);
document.getElementById("researchOutcomes").innerHTML=outcomeCards.map(x=>'<div class="metric"><div class="k">'+esc(x[0])+'</div><strong>'+esc(x[1])+'</strong></div>').join("");
const coverage=d.research_field_coverage||{};
const featureDefs=[
["Cross-sectional relative strength","Five-day return percentile versus the current scanned stock universe, SPY and IWM; rank trend appears after five days of history.",["relative_strength_vs_spy","relative_strength_vs_iwm","rs_rank","universe_rs_percentile","universe_rs_rank_change_5d"]],
 ["Market breadth context","Share of scanned stock candidates above their 20-day average, positive over five days, and breaking 20-day highs. Not full-exchange breadth.",["market_breadth"]],
 ["Weekly breakout confluence","Weekly EMA10/EMA20 trend confirmation, daily/weekly breakout confluence, and highest resistance level supported by available history.",["weekly_confirmation","weekly_breakout_confluence","breakout_level_class"]],
["Same-time-of-day relative volume","Current 15-minute bar volume versus the median for the same clock-time across prior sessions.",["intraday_rvol_same_time"]],
["Liquidity and spread","Average dollar volume and observed bid/ask spread; missing spread data is explicitly shown.",["dollar_volume","liquidity_status"]],
["Opening range and retest","15-minute opening range, upside/downside break and retest status. 5m/30m bars are not currently sourced.",["opening_range_status"]],
["Anchored VWAP","Session-open anchored VWAP and price positioning; catalyst/swing anchors remain future extensions.",["anchored_vwap_status","anchored_vwap"]],
["ADX / DMI","14-period ADX, +DI and -DI from daily OHLC bars.",["adx","plus_di","minus_di","adx_dmi_status"]],
["Volatility contraction","ATR contraction versus prior ATR and 10-day base tightness.",["volatility_contraction_status","atr_contraction_pct","base_tightness_pct"]],
["Catalyst and dilution","Headline catalyst classification plus SEC financing/offering flags; filing text still requires human review.",["catalyst_dilution_status","dilution_flags"]],
["Float rotation","Session volume divided by reported public float; float freshness and provider coverage must be checked.",["float_rotation_pct","float_shares"]],
["Premarket activity","Premarket gap, high/low and same-time historical premarket relative volume.",["premarket_gap_pct","premarket_rvol_same_time"]],
["Cumulative RVOL","Session cumulative volume compared with prior sessions at the same clock time.",["cumulative_rvol_same_time"]],
["Volume acceleration","Recent three-bar average volume versus the preceding three bars.",["volume_acceleration_3bar"]],
["Intraday structure","Higher highs/higher lows, pullback from session high and premarket-high reclaim.",["higher_highs_higher_lows","pullback_from_session_high_pct","premarket_high_reclaimed"]],
["Early runner research score","Transparent feature score for ranking research candidates; not a probability or BUY trigger.",["early_runner_research_score","early_runner_score_components"]],
["Early-warning engine","Completed-candle breakout proximity, liquidity sweep/reclaim, accumulation, volatility compression and timeframe alignment; included in the offline BUY intelligence blend.",["breakout_proximity_pct","liquidity_sweep_detected","accumulation_signal_score","multi_timeframe_alignment","volatility_compression_percentile"]],
["Unified BUY intelligence","Coverage-aware formation, momentum, unusual activity, catalyst, historical and early-warning evidence. Paper pilot only; the hybrid blend remains capped at 20%.",["buy_intelligence","score_model"]],
["Crypto Runner Quality Score (CRQS)","Crypto-specific quality score, tier, component breakdown, entry penalty, and data-coverage flags.",["crqs","crqs_tier","crqs_components","crqs_data_coverage","crqs_entry_score","crqs_entry_penalty","crqs_features"]]
];
const features=featureDefs.map(([name,description,keys])=>{const covered=Math.max(0,...keys.map(k=>Number(coverage[k]||0)));const total=all.length;const status=covered>0?"Collecting · "+covered+"/"+total+" records":"No measurements saved yet";return [name,description,status];});
document.getElementById("features").innerHTML=features.map(x=>'<div class="feature"><b>'+esc(x[0])+'</b><p>'+esc(x[1])+'</p><span class="status '+(x[2].startsWith("Existing")||x[2].startsWith("Collecting")?"partial":"")+'">'+esc(x[2])+'</span></div>').join("");
const money=v=>{const n=Number(v);return Number.isFinite(n)&&n>0?n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:6}):"—"};const arr=v=>Array.isArray(v)?v.filter(Boolean).map(esc).join(" · "):esc(v||"—");
const level=(o,k)=>money(o&&o[k]);
const buys=(d.buy_signals||[]).filter(x=>x.status==="active").sort((a,b)=>(b.at||"").localeCompare(a.at||""));
document.getElementById("buyLedger").innerHTML=buys.length?buys.map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b><br><span class="muted">'+esc(x.at||"")+'</span></td><td><span class="pill">'+esc(x.discovery_source==="research_layer"?"Intelligence-tagged":"Core BUY engine")+'</span></td><td><span class="pill">'+esc(x.score_model||"legacy_fallback")+'</span><br><span class="muted">Legacy '+esc(x.legacy_score??"—")+' · Intel '+esc((x.buy_intelligence&&x.buy_intelligence.score)??"—")+'</span></td><td>'+arr(x.research_signal_matches&&x.research_signal_matches.length?x.research_signal_matches:x.reasons)+'</td><td>'+money(x.entry_low)+' – '+money(x.entry_high)+'</td><td class="amber">'+level(x,"stop_loss")+'</td><td class="green">'+level(x,"target_1")+'</td><td class="green">'+level(x,"target_2")+'</td><td>'+esc(x.reward_risk_ratio?x.reward_risk_ratio+":1":"—")+'</td><td>'+esc(x.status||"active")+'</td></tr>').join(""):'<tr><td colspan="10" class="empty">No active official BUY signals are recorded right now. This is not a statement that the market has no opportunities.</td></tr>';
const researchBuys=all.filter(x=>Array.isArray(x.research_signal_matches)&&x.research_signal_matches.length).sort((a,b)=>(b.last_seen||"").localeCompare(a.last_seen||""));
document.getElementById("researchBuyLedger").innerHTML=researchBuys.length?researchBuys.map(x=>{const lv=x.levels||{};return '<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b></td><td>'+arr(x.research_signal_matches)+'</td><td><span class="pill">'+esc(x.last_stage||"—")+'</span></td><td>'+money(lv.watch_entry_low)+' – '+money(lv.watch_entry_high)+'</td><td class="amber">'+money(lv.stop_loss||lv.invalidation_level)+'</td><td class="green">'+money(lv.target_1)+'</td><td class="green">'+money(lv.target_2)+'</td><td>'+esc(x.research_setup_status||"Observed match · not a trade trigger")+'</td></tr>';}).join(""):'<tr><td colspan="8" class="empty">No intelligence matches recorded yet. The additional indicators still need to be wired into candidate discovery; Jarvis will not invent signal attribution.</td></tr>';const readinessRecords=all.filter(x=>x.buy_readiness&&typeof x.buy_readiness==="object").sort((a,b)=>(Number(b.buy_readiness.percent||0)-Number(a.buy_readiness.percent||0)));
const gateNames={live_data_quality:"Live data quality",score_82:"Score threshold",volume_confirmed:"Volume confirmed",breakout_confirmed:"Breakout confirmed",two_technical_confirmations:"2 technical confirmations",market_not_risk_off:"Market regime acceptable",fresh_intraday_confirmation:"Fresh intraday data",five_bar_intraday_trend_up:"5-bar trend up",no_risk_flags:"No risk flags",not_chase_risk:"Not chase-risk",price_verified:"Price verified",volume_1_8x:"Relative volume ≥1.8x","24h_breakout":"24h breakout",above_24h_average:"Above 24h average",positive_hourly_momentum:"Positive hourly momentum",not_extended:"Not extended"};
document.getElementById("buyReadinessLedger").innerHTML=readinessRecords.length?readinessRecords.map(x=>{
const r=x.buy_readiness||{},checks=r.checks||{},passed=Object.keys(checks).filter(k=>checks[k]).map(k=>gateNames[k]||k.replace(/_/g," ")),missing=Array.isArray(r.missing_conditions)?r.missing_conditions:[],verified=x.price_validation_status==="verified"||x.price_validation_passed===true;
return '<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b><br><span class="muted">'+esc(x.last_stage||x.stage||"—")+'</span></td><td><b>'+esc(String(r.passed??0)+"/"+String(r.total??0)+" · "+String(r.percent??0)+"%")+'</b><div style="height:7px;background:#24364a;border-radius:8px;overflow:hidden;margin-top:6px"><div style="height:7px;width:'+Math.max(0,Math.min(100,Number(r.percent)||0))+'%;background:var(--green);border-radius:8px"></div></div></td><td>'+(verified?'<span class="pill" style="color:var(--green)">Verified</span>':'<span class="pill" style="color:var(--amber)">Unverified · Watch only</span>')+'</td><td>'+(passed.length?passed.map(esc).join(" · "):"—")+'</td><td>'+(missing.length?missing.map(esc).join(" · "):'<span style="color:var(--green)">All readiness gates passed</span>')+'</td></tr>';
}).join(""):'<tr><td colspan="5" class="empty">Readiness records will appear after a scan runs with the new tracking fields saved.</td></tr>';
document.getElementById("ledger").innerHTML=all.length?all.sort((a,b)=>(b.last_seen||"").localeCompare(a.last_seen||"")).slice(0,100).map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b></td><td><span class="pill">'+esc(x.last_stage||"—")+'</span></td><td>'+esc(x.price_validation_status||"unknown")+'<br><span class="muted">Type: '+esc((x.data_quality&&x.data_quality.data_type)||"unknown")+'</span><br><span class="muted">'+esc((x.data_quality&&x.data_quality.verification_status)||"unknown")+' · age '+esc((x.data_quality&&x.data_quality.age_seconds)!=null?Math.round(x.data_quality.age_seconds)+"s":"—")+'</span><br><span class="muted">Observed: '+esc((x.data_quality&&x.data_quality.observed_at)||"unknown")+'</span><br><span class="muted">Received: '+esc((x.data_quality&&x.data_quality.received_at)||"unknown")+' · '+esc((x.data_quality&&x.data_quality.session)||"session unknown")+'</span><br><span class="muted">'+esc(x.price_source||"source not recorded")+'</span><br><span class="muted">'+esc(x.price_validation_note||"Validation detail not recorded")+'</span><br><span class="muted">Massive: '+esc(x.massive_quote_status||"not reported")+'</span></td><td>'+pick(x,["relative_strength_vs_spy","relative_strength_pct","rs_rank"])+'</td><td>'+pick(x,["intraday_rvol_same_time","same_time_relative_volume"])+'</td><td>'+pick(x,["anchored_vwap_status","opening_range_status","vwap_reclaim"])+'</td><td>'+pick(x,["adx","adx_dmi_status"])+'</td><td>'+pick(x,["spread_pct","dollar_volume","liquidity_status"])+'</td><td>'+researchDetail(x.catalyst_dilution_status??x.dilution_flags??x.catalysts)+'</td></tr>').join(""):'<tr><td colspan="9" class="empty">No candidate records available.</td></tr>';}
fetch("/intelligence/data").then(r=>{if(!r.ok)throw Error();return r.json()}).then(render).catch(()=>{document.getElementById("updated").textContent="Intelligence data unavailable";document.getElementById("ledger").innerHTML='<tr><td colspan="9" class="empty">Could not load saved intelligence data. Try again after the next successful sync.</td></tr>';});
</script></body></html>"""))


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
    massive_key=bool(os.getenv("MASSIVE_API_KEY","").strip())
    webull_keys=bool(os.getenv("WEBULL_APP_KEY","").strip() and os.getenv("WEBULL_APP_SECRET","").strip())
    return {"synced_at":synced,"last_scan_completed_at":completed,"scan_run_id":run_id,
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
