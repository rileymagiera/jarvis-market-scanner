import os
import json
import hmac
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import numpy as np
import pandas as pd
import requests
from io import StringIO
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

def discover_stock_symbols(limit: int = 70) -> list[str]:
    """Discover US small/mid-cap momentum names across Yahoo screens concurrently."""
    found = {}
    # Run independent screens concurrently: the previous serial 6 x 15s timeout
    # could consume 90 seconds before the actual symbol scan even began.
    screens = (
        "small_cap_gainers",
        "aggressive_small_caps",
        "most_shorted_stocks",
        "growth_technology_stocks",
        "day_gainers",
        "most_actives",
    )

    def fetch_screen(screen: str) -> list[dict]:
        try:
            response = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"formatted": "false", "scrIds": screen, "count": 100, "start": 0},
                headers={"User-Agent": "JarvisMarketScanner/2.0"},
                timeout=12,
            )
            response.raise_for_status()
            results = (response.json().get("finance", {}).get("result") or [])
            return results[0].get("quotes", []) if results else []
        except Exception as exc:
            # Discovery is additive: one unavailable screen must not stop the scan.
            print(f"Yahoo discovery screen {screen} failed: {type(exc).__name__}")
            return []

    with ThreadPoolExecutor(max_workers=len(screens)) as pool:
        screen_quotes = pool.map(fetch_screen, screens)

    for quotes in screen_quotes:
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
    frame = frame.dropna(subset=["Close", "Volume"]).sort_values("Date").tail(90)
    if len(frame) < 22 or safe_float(frame["Volume"].tail(20).mean()) <= 0:
        raise RuntimeError(f"Stooq returned insufficient usable daily history for {symbol}")
    return frame.reset_index(drop=True)


def stooq_latest_quote(symbol: str) -> dict:
    """Fetch an independent Stooq quote for cross-provider validation."""
    response = HTTP.get(
        "https://stooq.com/q/l/",
        params={"s": f"{symbol.lower()}.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"},
        timeout=12,
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
    return {"price": price, "as_of": stamp.isoformat(), "age_minutes": round(age, 1)}


MASSIVE_REQUEST_LOCK = threading.Lock()
MASSIVE_LAST_REQUEST_AT = 0.0
MASSIVE_REQUESTS_THIS_PROCESS = 0
MASSIVE_MAX_REQUESTS_PER_SCAN = 10
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
    """Yahoo primary, Massive free-tier EOD backup, then Stooq emergency fallback."""
    ticker = ticker or yf.Ticker(symbol)
    try:
        frame = ticker.history(period="3mo", interval="1d", auto_adjust=True)
        if not frame.empty and len(frame.dropna(subset=["Close", "Volume"])) >= 22:
            return frame, "Yahoo Finance"
        raise RuntimeError("Yahoo returned insufficient daily history")
    except Exception as primary_error:
        massive_error = None
        try:
            return massive_daily_history(symbol), "Massive daily aggregates (free-tier EOD/delayed; no intraday)"
        except Exception as exc:
            massive_error = exc
        try:
            return stooq_daily_history(symbol), "Stooq public daily data (emergency backup; may be delayed)"
        except Exception as stooq_error:
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
                hist = yf.Ticker(symbol).history(period="2mo", interval="1d", auto_adjust=True)
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
    raw_score = int(item.get("score") or 0)
    adjusted = max(0, min(100, raw_score + adjustment))
    item["raw_score"] = raw_score
    item["score"] = adjusted
    item["market_regime"] = regime["regime"]
    item["market_score_adjustment"] = adjustment

    if item.get("asset_type") == "stock" and not item.get("price_validation_passed", False):
        item["stage"] = "PRICE UNVERIFIED — WATCH ONLY"
        item["signal"] = item["stage"]
        item["signal_blocked_reason"] = item.get("price_validation_note", "Independent quote validation unavailable")
        item["stage_reasons"] = ["Fresh independent price confirmation is required before an actionable alert."]
        return

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
    try:
        # Stooq is a daily-only backup; do not pretend it supplies intraday confirmation.
        intraday = ticker.history(period="5d", interval="15m", prepost=True, auto_adjust=True) if price_provider == "Yahoo Finance" else pd.DataFrame()
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
    has_webull_credentials = bool(
        os.getenv("WEBULL_APP_KEY", "").strip()
        and os.getenv("WEBULL_APP_SECRET", "").strip()
    )
    webull_enabled = has_webull_credentials and WEBULL_QUOTE_ENABLED

    if (intraday_fresh and intraday_price > 0) or webull_enabled:
        try:
            # Stooq can independently cross-check Webull even when Yahoo is down.
            secondary_quote = stooq_latest_quote(symbol)
            quote_difference_pct = (
                abs(intraday_price - secondary_quote["price"])
                / ((intraday_price + secondary_quote["price"]) / 2) * 100
            )
        except Exception:
            secondary_quote = None

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
            validation_note = "Yahoo and Stooq live quotes disagree by more than 3%; Webull not accepted"
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
        validation_note = "Fresh Yahoo intraday quote available, but independent Stooq validation unavailable and Webull did not return a quote"
    elif webull_enabled and not webull_quote:
        validation_note = "Yahoo intraday quote unavailable; Webull quote request also unavailable or not entitled"

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

    if score < 15:
        return None
    news = stock_news(symbol)
    fundamentals = stock_fundamental_snapshot(ticker) if score >= 35 else {"status": "not_fetched_for_low_score"}
    risk_flags = []
    if not validation_passed:
        risk_flags.append("Price not independently verified; actionable alerts blocked")
    if any(item.get("form") in {"S-1", "S-3", "424B1", "424B3", "424B5"} for item in news):
        risk_flags.append("Potential offering/registration filing detected; review the SEC document")
    return {
        "symbol": symbol,
        "asset_type": "stock",
        "price": round(price, 4),
        "price_as_of": selected_price_as_of or pd.Timestamp(frame.index[-1]).isoformat(),
        "price_freshness": selected_price_freshness,
        "price_validation_passed": validation_passed,
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
        "market_session": equity_session,
        "daily_change_pct": round(daily_change, 2),
        "session_change_pct": round(daily_change, 2) if validation_passed else None,
        "five_day_change_pct": round(five_day_change, 2),
        "relative_volume": round(relative_volume, 2),
        "intraday_change_pct": round(intraday_change, 2),
        "intraday_relative_volume": round(intraday_relative_volume, 2),
        "intraday_breakout": bool(intraday_breakout),
        "breakout_20d": bool(breakout),
        "above_20d_average": bool(trend_up),
        "technical_analysis": technical,
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
    ticker_payload = ticker_response.json()    if ticker_payload.get("error"):
        raise RuntimeError("Kraken Ticker: " + "; ".join(ticker_payload["error"]))
    ticker_rows = ticker_payload.get("result") or {}
    if not ticker_rows:
        raise RuntimeError(f"Kraken returned no ticker for {base}")    ticker = next(iter(ticker_rows.values()))
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

    # Crypto uses the same evidence-family principle as stocks: one momentum
    # score, one volume score, one breakout score, and one trend score.
    momentum_points = max(
        30 if change_1h >= 1.5 else 16 if change_1h >= 0.5 else 0,
        30 if change_24h >= 5 else 16 if change_24h >= 2 else 0,
    )
    volume_points = 25 if relative_volume >= 2.5 else 18 if relative_volume >= 1.8 else 9 if relative_volume >= 1.3 else 0
    breakout_points = 25 if breakout else 0
    trend_points = 20 if trend_up else 0
    score_components = {
        "momentum": momentum_points,
        "volume": volume_points,
        "breakout": breakout_points,
        "trend": trend_points,
    }
    score = min(100, sum(score_components.values()))
    if score < 15:
        return None

    return {
        "symbol": symbol,
        "asset_type": "crypto",
        "price": price,
        "price_as_of": datetime.now(timezone.utc).isoformat(),
        "price_freshness": "live_ticker_snapshot",
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



APP_DIR = Path(__file__).resolve().parent
PAPER_STATE = APP_DIR / "paper_tracker.json"
PAPER_LIFETIME_STATE = APP_DIR / "paper_tracker_lifetime.json"
BUY_SIGNAL_STATE = APP_DIR / "buy_signal_history.json"
HISTORY_SNAPSHOT = APP_DIR / "history_snapshot.json"


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
.command-header{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:10px 2px 22px;border-bottom:1px solid #293b50}
.brand-lockup{display:flex;align-items:center;gap:13px}.brand-mark{width:48px;height:48px;border:1px solid #527da3;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,#244767,#102236);font-size:26px;font-weight:900;color:#d9f1ff;box-shadow:0 0 28px #5cbcff20}
.command-header h1{font-size:clamp(27px,4vw,36px);letter-spacing:.08em;margin:0;line-height:1}.title-dot{color:#6ed8d0}.brand-sub{font-size:9px;letter-spacing:.12em;color:#91a7bd;margin-top:7px}
.header-right{text-align:right}.live-status{font-size:11px;color:#b8c9da;border:1px solid var(--line);background:#111e2b;padding:8px 11px;border-radius:999px;display:inline-flex;align-items:center;max-width:100%;white-space:normal}
.live-status:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:7px;box-shadow:0 0 10px #63d9a077;flex:0 0 auto}
.header-right .tiny{font-size:9px;letter-spacing:.1em;margin-top:8px}
.hero-panel{position:relative;overflow:hidden;display:grid;grid-template-columns:1.2fr .8fr;min-height:245px;margin:22px 0 28px;padding:28px 34px;border:1px solid #294d65;border-radius:20px;background:radial-gradient(ellipse at 78% 48%,#17445a 0%,#14283a 36%,#101b29 78%);box-shadow:0 18px 50px #0004}
.hero-copy{position:relative;z-index:2;align-self:center;max-width:520px}.hero-copy h2{font-size:clamp(29px,4vw,45px);line-height:1.04;letter-spacing:-.045em;margin:5px 0 12px}.hero-copy h2 span{color:#78d9d0}.hero-copy p{font-size:13px;color:#b4c7d8;max-width:430px;margin:0;line-height:1.65}
.radar-art{position:relative;align-self:center;justify-self:center;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,#183b50 0%,#12283a 48%,#0d1b28 70%);border:1px solid #4e8498;box-shadow:0 0 55px #43b6bd22,inset 0 0 35px #56c9cf18}
.radar-ring{position:absolute;border:1px solid #4f9ca277;border-radius:50%;inset:15%}.ring-two{inset:30%}.ring-three{inset:45%}.radar-art:before,.radar-art:after{content:"";position:absolute;background:#4f9ca255}.radar-art:before{height:1px;left:7%;right:7%;top:50%}.radar-art:after{width:1px;top:7%;bottom:7%;left:50%}
.radar-sweep{position:absolute;inset:9%;border-radius:50%;background:conic-gradient(from 5deg,transparent 0deg,transparent 290deg,#5ed8d044 345deg,#5ed8d088 360deg);clip-path:circle(50%);transform:rotate(20deg)}
.radar-core{position:absolute;left:48%;top:48%;width:9px;height:9px;border-radius:50%;background:#8fffe0;box-shadow:0 0 18px 6px #5ce7d477}
.radar-label{position:absolute;font-size:8px;letter-spacing:.12em;color:#a8d6df;background:#102637;border:1px solid #356175;padding:4px 6px;border-radius:5px}.radar-label-one{top:15%;right:-8%}.radar-label-two{bottom:16%;left:-10%}.radar-label-three{bottom:3%;right:15%}
.radar-section{margin-top:0;padding-top:2px}.section-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:14px}.section-heading h2{font-size:22px;margin:0 0 4px}
.radar-stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;border:1px solid #293d50;border-radius:12px;overflow:hidden;background:#293d50;margin-bottom:14px}
.radar-stat{padding:12px 15px;background:#111e2b;min-width:0}.radar-stat strong{display:block;font-size:23px;line-height:1.1;font-weight:800;font-variant-numeric:tabular-nums}.radar-stat span{display:block;color:#8fa7bb;font-size:9px;letter-spacing:.1em;margin-top:6px}
.opportunity-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
.buy-floor{margin:0 0 26px;padding:18px;border:1px solid #356d68;border-radius:16px;background:linear-gradient(135deg,#112b2b,#101d29 72%)}.buy-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.buy-card{padding:14px;border:1px solid #387b69;border-radius:12px;background:#102a27;min-width:0}.buy-card-top{display:flex;justify-content:space-between;gap:10px;align-items:center}.buy-tag{font-size:10px;font-weight:850;letter-spacing:.12em;color:#9cf0c8;border:1px solid #438b70;border-radius:99px;padding:4px 8px}.buy-symbol{font-size:20px;font-weight:850}.buy-detail{font-size:11px;color:#c2d6d2;line-height:1.5;margin-top:8px}.buy-return{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums}.buy-meta{font-size:10px;color:#93b2ac;margin-top:5px}.op-card{background:linear-gradient(145deg,#172b3b,#101c29);border:1px solid #2c4659;border-radius:14px;padding:15px;min-width:0;transition:border-color .15s,transform .15s}.op-card:hover{border-color:#5c9da6;transform:translateY(-2px)}
.op-card.featured{grid-column:span 2;display:grid;grid-template-columns:1.1fr 1fr;column-gap:20px;border-color:#3e8d8e;background:linear-gradient(115deg,#18384a,#132637 65%,#10212e);box-shadow:inset 3px 0 #66d8cc,0 10px 28px #0003}
.op-card.featured .op-card-top{grid-column:1/-1}.op-card.featured .op-symbol{font-size:27px}.op-card.featured .op-reason{align-self:center;font-size:13px}.op-card.featured .op-metrics{align-self:center}
.op-card-top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}.op-symbol{font-size:19px;font-weight:850;letter-spacing:-.02em}.op-kind{font-size:9px;color:#9db1c3;text-transform:uppercase;letter-spacing:.13em}
.op-stage{display:inline-flex;border:1px solid #48617b;background:#20364b;color:#d5e8fa;border-radius:999px;padding:5px 9px;font-size:9px;line-height:1.3;letter-spacing:.03em}
.op-stage.action{border-color:#397e69;background:#16392f;color:#9ae5c3}.op-stage.late{border-color:#875e42;background:#392719;color:#f1bd91}
.op-metrics{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0}.op-metric strong{display:block;font-size:17px;font-variant-numeric:tabular-nums}.op-metric span{font-size:9px;color:#91a7ba;letter-spacing:.04em}
.op-reason{font-size:11px;color:#c4d2e0;line-height:1.5;margin-top:8px}.op-risk{font-size:10px;color:#ffb0a6;margin-top:8px}.op-front-quality{border:1px solid #34566a;border-radius:11px;padding:10px 11px;margin:10px 0;background:linear-gradient(135deg,#102a38,#102131)}.op-front-quality-top{display:flex;justify-content:space-between;align-items:center;gap:8px}.op-front-quality-top>div>span,.op-front-plan span{display:block;font-size:9px;letter-spacing:.08em;color:#91b2c4}.op-front-quality-top strong{display:block;font-size:25px;line-height:1.1;color:#e6f5fb;font-variant-numeric:tabular-nums}.op-front-quality-top strong small{font-size:12px;color:#8faabd}.op-quality-label{font-size:9px;font-weight:800;letter-spacing:.08em;color:#9be2d1;border:1px solid #38695f;background:#15352f;border-radius:99px;padding:5px 8px}.op-quality-track{height:5px;background:#263b4b;border-radius:9px;overflow:hidden;margin:8px 0}.op-quality-track i{display:block;height:100%;background:linear-gradient(90deg,#e0a46b,#76d6bd);border-radius:9px}.op-evidence-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 12px}.op-evidence-item>div{display:flex;justify-content:space-between;gap:5px;font-size:8px;color:#9ab2c3;letter-spacing:.04em}.op-evidence-item b{color:#d8e7f0;font-variant-numeric:tabular-nums}.op-evidence-item i{display:block;height:3px;background:#263a49;border-radius:8px;overflow:hidden;margin-top:3px}.op-evidence-item em{display:block;height:100%;background:#65cbbb;border-radius:8px}.op-front-quality.unavailable{color:#e5bd92;background:#30251d;border-color:#705238}.op-front-quality.unavailable>div{font-weight:800}.op-front-quality.unavailable span{font-size:9px;letter-spacing:.08em}.op-front-quality.unavailable strong{display:block;font-size:19px}.op-front-quality.unavailable small{color:#c8ad91;font-size:10px}.op-front-plan{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}.op-front-plan>div{min-width:0;background:#0b1824;border:1px solid #31495d;border-radius:9px;padding:9px}.op-front-plan b{display:block;margin-top:4px;font-size:12px;color:#e4eff8;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.op-front-plan-note{font-size:10px;color:#e7b98e;margin:8px 0}.op-card.featured{display:block}.op-flippable{perspective:1100px;cursor:pointer;outline:none}.op-flippable:focus-visible{outline:2px solid var(--accent);outline-offset:4px}.op-flip-inner{display:grid;transform-style:preserve-3d;transition:transform .55s cubic-bezier(.2,.75,.25,1);min-height:100%;position:relative}.op-flippable.is-flipped .op-flip-inner{transform:rotateY(180deg)}.op-face{grid-area:1/1;backface-visibility:hidden;-webkit-backface-visibility:hidden;min-width:0}.op-face-back{transform:rotateY(180deg);display:flex;flex-direction:column;gap:8px}.op-back-title{font-size:10px;letter-spacing:.12em;color:#8edbd5;font-weight:800}.op-plan-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.op-plan-cell{border:1px solid #31495d;border-radius:9px;padding:8px;background:#0b1824}.op-plan-cell strong{display:block;font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.op-plan-cell span{display:block;color:#91a7ba;font-size:9px;letter-spacing:.04em}.op-confidence{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;border-radius:9px;background:#193447;border:1px solid #3b657a}.op-confidence strong{font-size:18px}.op-confidence small{display:block;color:#a9c0d0;font-size:9px}.op-confidence.high{border-color:#397e69;background:#16392f}.op-confidence.low{border-color:#875e42;background:#392719}.op-back-note{font-size:10px;color:#a9bdce;line-height:1.45}.op-front-explanation{font-size:10px;color:#a9bdce;line-height:1.45;margin:0 0 9px;padding:9px 10px;border:1px solid #31564f;border-radius:9px;background:#102824}.op-front-explanation>div+div{margin-top:6px;color:#93b8aa}.target-math{border:1px solid #35546a;border-radius:12px;padding:11px;background:linear-gradient(145deg,#102334,#0c1926);display:grid;gap:9px}.target-math-head{display:flex;justify-content:space-between;align-items:center;gap:8px}.target-math-head b{font-size:11px;letter-spacing:.09em;color:#d7eaf7}.target-math-head span{font-size:9px;color:#8eb6c9;border:1px solid #34546b;border-radius:99px;padding:4px 7px}.target-math-inputs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.target-math-input{padding:7px 8px;border-radius:8px;background:#0a1722;border:1px solid #263f52;min-width:0}.target-math-input span{display:block;font-size:8px;color:#8faabd;letter-spacing:.06em}.target-math-input strong{display:block;font-size:12px;color:#e3eef7;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin-top:3px}.target-math-risk{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;background:#2b241b;border:1px solid #68513a;border-radius:8px}.target-math-risk span{font-size:9px;color:#d8b994}.target-math-risk strong{font-size:13px;color:#ffdbad;font-variant-numeric:tabular-nums}.target-math-level{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;padding:9px 10px;border-radius:9px;border:1px solid #315c54;background:#102b29}.target-math-level.level-two{border-color:#4b547b;background:#1c2238}.target-math-level small{display:block;color:#9bb9b2;font-size:8px;letter-spacing:.07em}.target-math-level.level-two small{color:#b1b8dc}.target-math-equation{font-size:10px;color:#d6e3ed;line-height:1.5;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin-top:3px}.target-math-result{text-align:right}.target-math-result strong{font-size:16px;color:#a6ebc9;font-variant-numeric:tabular-nums}.target-math-level.level-two .target-math-result strong{color:#c4caff}.target-math-result span{display:block;font-size:8px;color:#a9bdce}.target-math-foot{font-size:9px;color:#9bb0c1;line-height:1.45}.target-math-foot b{color:#d3e1eb}.op-flip-hint{font-size:9px;color:#86c9d1;margin-top:10px;letter-spacing:.04em}.op-empty{border:1px dashed #40546a;border-radius:12px;padding:20px;color:var(--muted);font-size:13px;grid-column:1/-1}.footnote{margin-top:13px}.timeline-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:10px}.timeline-card{border:1px solid #2c4659;border-radius:12px;padding:13px;background:linear-gradient(145deg,#172b3b,#101c29);min-width:0}.timeline-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.timeline-symbol{font-size:15px;font-weight:850}.timeline-stage{font-size:9px;border:1px solid #48617b;border-radius:99px;padding:4px 7px;color:#d5e8fa}.timeline-events{margin-top:9px;border-left:1px solid #426077;padding-left:10px;display:grid;gap:7px}.timeline-event{font-size:10px;color:#b6c8d8;line-height:1.4}.timeline-event b{color:#e4eff8}.settings-panel{display:flex;flex-wrap:wrap;gap:12px;align-items:end;border:1px solid var(--line);border-radius:13px;background:#101b28;padding:14px}.settings-panel label{display:grid;gap:5px;color:#aebfd0;font-size:11px;min-width:170px}.settings-panel label select{width:100%}.settings-panel .setting-check{display:flex;align-items:center;gap:7px;min-width:190px}.settings-panel .setting-check input{flex:0 0 auto;min-width:0;min-height:0;accent-color:#83b8ff}.settings-panel button{border:1px solid #40536a;background:#1b2b3c;color:var(--text);border-radius:9px;padding:10px 12px;min-height:42px;font:inherit;font-size:11px;cursor:pointer}.compact-mode .op-card,.compact-mode .timeline-card,.compact-mode .buy-card{padding:10px}.compact-mode .op-metrics{gap:10px;margin:6px 0}.compact-mode .timeline-events{gap:4px}
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
@media(max-width:700px){.command-header{align-items:flex-start;flex-direction:column}.header-right{text-align:left}.hero-panel{grid-template-columns:1fr;min-height:0;padding:24px}.radar-art{width:155px;height:155px;margin:20px auto 0}.radar-label{font-size:7px}.radar-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.opportunity-grid,.buy-grid{grid-template-columns:1fr}.op-card.featured{grid-column:auto;display:block}.op-card.featured .op-symbol{font-size:22px}.section-heading{align-items:flex-start;flex-direction:column}.archive>summary{align-items:flex-start;flex-wrap:wrap}.summary-note{width:100%}.archive-body{padding:0 10px 14px}}

@media(max-width:700px){main{width:calc(100% - 24px);padding:22px 0 40px}.cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.card,.panel{padding:12px}.section{margin-top:24px;padding-top:18px}th,td{padding:10px;font-size:11px}#updated{max-width:100%;white-space:normal}}
@media(max-width:380px){.cards{grid-template-columns:1fr 1fr}.value{font-size:19px}}
</style></head><body><main><header class="command-header"><div class="brand-lockup"><div class="brand-mark">J</div><div><div class="eyebrow">MARKET INTELLIGENCE SYSTEM</div><h1>JARVIS<span class="title-dot">.</span></h1><div class="brand-sub">EARLY-MOVE DETECTION · STOCKS + DIGITAL ASSETS</div></div></div><div class="header-right"><div class="live-status" id="updated">Connecting to saved history…</div><div class="tiny">RESEARCH ONLY · NO AUTOMATED TRADES</div></div></header><nav class="top-tabs" style="display:flex;gap:8px;margin:18px 0 22px"><a href="/history" style="padding:8px 14px;border:1px solid #293b50;border-radius:10px;color:#edf3fa;text-decoration:none">Overview</a><a href="/research" style="padding:8px 14px;border:1px solid #47617d;border-radius:10px;background:#20344a;color:#edf3fa;text-decoration:none">Research</a><a href="#dashboard-settings" style="padding:8px 14px;border:1px solid #293b50;border-radius:10px;color:#edf3fa;text-decoration:none">Customize</a></nav><section class="hero-panel"><div class="hero-copy"><div class="eyebrow">THE WATCHFLOOR</div><h2>Find the move<br><span>before the crowd.</span></h2><p>Jarvis is watching for developing momentum, unusual activity, and early technical shifts. Here’s what has earned a closer look.</p></div><div class="radar-art" aria-hidden="true"><div class="radar-ring ring-one"></div><div class="radar-ring ring-two"></div><div class="radar-ring ring-three"></div><div class="radar-sweep"></div><div class="radar-core"></div><span class="radar-label radar-label-one">MOMENTUM</span><span class="radar-label radar-label-two">VOLUME</span><span class="radar-label radar-label-three">BREAKOUT</span></div></section><section class="buy-floor"><div class="section-heading"><div><div class="eyebrow">STRICT SIGNAL LOG</div><h2>BUY signals</h2><div class="muted">Only setups that pass Jarvis's full entry filter. Paper-tracked; no trades are placed.</div></div><div class="tiny" id="buy-summary">Waiting for saved signal history</div></div><div id="buy-signals" class="buy-grid"><div class="op-empty">No BUY signals recorded yet. Jarvis will only show one when every required condition passes.</div></div></section><section class="radar-section"><div class="section-heading"><div><div class="eyebrow">LIVE WATCHLIST</div><h2>Opportunity radar</h2><div class="muted">Candidates are ranked by setup stage—not just today's biggest gainers.</div></div><div class="tiny">VERIFY PRICE + RISK BEFORE ANY DECISION</div></div><div id="radar-stats" class="radar-stats"><div class="radar-stat"><strong>—</strong><span>ON RADAR</span></div><div class="radar-stat"><strong>—</strong><span>EARLY / DEVELOPING</span></div><div class="radar-stat"><strong>—</strong><span>ACTIONABLE GRADE</span></div><div class="radar-stat"><strong>—</strong><span>PRICE VERIFIED</span></div></div><div id="opportunities" class="opportunity-grid"><div class="empty">Loading candidates…</div></div><div class="tiny footnote">Returns begin at first verified detection, not trade fills. Unverified prices are not treated as confirmed performance. Jarvis never places trades.</div></section>
<section class="section" id="radar-timeline"><div class="section-heading"><div><div class="eyebrow">OPPORTUNITY LIFECYCLE</div><h2>Radar timeline</h2><div class="muted">A compact view of where candidates sit in the discovery-to-action lifecycle.</div></div><div class="tiny">Stage history is recorded by completed scans</div></div><div id="radar-timeline-cards" class="timeline-grid"><div class="op-empty">Waiting for candidate history…</div></div></section>
<section class="section" id="dashboard-settings"><div class="section-heading"><div><div class="eyebrow">PERSONAL WORKSPACE</div><h2>Dashboard settings</h2><div class="muted">These controls change the display only; scanner thresholds and signal calculations remain unchanged.</div></div></div><div class="settings-panel"><label>Radar filter<select id="radar-filter"><option value="all">All candidates</option><option value="developing">Developing and stronger</option><option value="actionable">Actionable / BUY only</option></select></label><label>Layout density<select id="layout-density"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label><label class="setting-check"><input type="checkbox" id="show-timeline"> Show Radar Timeline</label><label class="setting-check"><input type="checkbox" id="show-performance"> Show Performance &amp; Tracking Details</label><button type="button" id="reset-dashboard-settings">Reset preferences</button></div><div class="tiny" style="margin-top:9px">Preferences are saved in this browser on this device.</div></section>
<details class="archive" id="performance-archive"><summary><span>Performance & tracking details</span><span class="summary-note">Scorecards, history, and full candidate tables</span></summary><div class="archive-body"><h2 class="archive-title">Performance snapshot</h2><div class="cards" id="stats"></div><section class="section" id="target-scorecard"><h2>Level 1 / Level 2 target scorecard</h2><div class="muted">First observed sampled-price crossings of saved BUY targets. These are observations, not guaranteed fills or intraday execution records.</div><div class="cards" id="target-performance"></div><div class="scroll"><table><thead><tr><th>Signal</th><th>Entry</th><th>Level 1</th><th>Level 2</th><th>Stop</th><th>Time to L1</th><th>Time to L2</th></tr></thead><tbody id="target-tracking"></tbody></table></div></section>
<section class="section"><h2>Stock vs. crypto scoreboard</h2><div class="grid" id="assets"></div></section>
<section class="section"><h2>Performance leaderboard</h2><div class="muted">Top tracked candidates by return from first tracked price.</div><div class="scroll"><table><thead><tr><th>#</th><th>Candidate</th><th>First price</th><th>Latest</th><th>Return</th><th>Best</th><th>Worst</th></tr></thead><tbody id="leaders"></tbody></table></div></section>
<section class="section" id="candidate-ledger"><h2>All tracked candidates</h2><div class="toolbar"><input id="q" placeholder="Search ticker…"><select id="assetFilter"><option value="all">All assets</option><option value="stock">Stocks</option><option value="crypto">Crypto</option></select><select id="gradeFilter"><option value="all">All grades</option><option>BUY</option><option>ACTIONABLE SETUP</option><option>DEVELOPING RUNNER</option><option>DEVELOPING SETUP</option><option>EARLY SETUP</option><option>EARLY WATCH</option><option>LATE RUNNER — CHASE RISK</option><option>LOW-CONVICTION WATCH</option><option>PRICE UNVERIFIED — WATCH ONLY</option></select></div><div class="scroll"><table><thead><tr><th>Candidate</th><th>First seen</th><th>First price</th><th>Current grade</th><th>Price source / freshness</th><th>Price as of</th><th>Session move</th><th>Return since first verified</th><th>Best</th><th>Worst</th><th>Age</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="section"><h2>BUY signal performance</h2><div class="muted">Measured from the exact first BUY signal price. Outcomes are recorded at fixed elapsed-time checkpoints; this is a paper record, not executed fills.</div><div class="scroll"><table><thead><tr><th>Signal</th><th>First BUY</th><th>Entry</th><th>Latest return</th><th>1h</th><th>4h</th><th>1d</th><th>3d</th><th>7d</th><th>Score</th></tr></thead><tbody id="buy-history"></tbody></table></div></section><section class="section"><h2>Accuracy scorecard</h2><div class="muted">Current observed outcomes; legacy checkpoint totals may include stock observations made before independent quote validation.</div><div class="grid"><div class="panel"><h3>Return thresholds</h3><div id="thresholds"></div></div><div class="panel"><h3>Lifetime checkpoints</h3><div class="scroll"><table><thead><tr><th>Days</th><th>Observations</th><th>Wins</th><th>Losses</th><th>Average return</th></tr></thead><tbody id="checks"></tbody></table></div></div></div></section>
<section class="section"><h2>Grade movement history</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>From</th><th>To</th><th>When</th></tr></thead><tbody id="moves"></tbody></table></div></section>
<section class="section"><h2>Discovery reasons & catalysts</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>Signals</th><th>Latest headline / filing</th><th>Risk flags</th></tr></thead><tbody id="catalysts"></tbody></table></div></section>
<section class="section"><h2>Not in latest scan</h2><div class="tiny">Previously tracked, but absent from the latest scan. Last recorded prices may be stale; this is not a sell signal.</div><div class="scroll"><table><thead><tr><th>Candidate</th><th>Last seen</th><th>Last return</th><th>Best gain</th><th>Last grade</th></tr></thead><tbody id="faded"></tbody></table></div></section>
<p class="tiny section">Data may be delayed or incomplete. Candidate discovery is not the same as an actionable alert.</p></div></details></main><script>
const n=v=>Number(v||0),pct=v=>n(v).toFixed(2)+'%',cl=v=>n(v)>0?'pos':n(v)<0?'neg':'',e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"),date=v=>v?new Date(v).toLocaleString():'—';
const rank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1,'PRICE UNVERIFIED — WATCH ONLY':0};
const PREF_KEY='jarvisDashboardPrefsV1';const prefs={radarFilter:'all',density:'comfortable',showTimeline:true,showPerformance:true};try{Object.assign(prefs,JSON.parse(localStorage.getItem(PREF_KEY)||'{}'));}catch(_){}
function savePrefs(){try{localStorage.setItem(PREF_KEY,JSON.stringify(prefs));}catch(_){}document.body.classList.toggle('compact-mode',prefs.density==='compact');document.getElementById('radar-timeline').hidden=!prefs.showTimeline;document.getElementById('performance-archive').hidden=!prefs.showPerformance;}
function bindPrefs(){const radar=document.getElementById('radar-filter'),density=document.getElementById('layout-density'),timeline=document.getElementById('show-timeline'),performance=document.getElementById('show-performance');radar.value=prefs.radarFilter;density.value=prefs.density;timeline.checked=prefs.showTimeline;performance.checked=prefs.showPerformance;radar.addEventListener('change',()=>{prefs.radarFilter=radar.value;savePrefs();if(window.jarvisRenderData)render(window.jarvisRenderData);});density.addEventListener('change',()=>{prefs.density=density.value;savePrefs();});timeline.addEventListener('change',()=>{prefs.showTimeline=timeline.checked;savePrefs();});performance.addEventListener('change',()=>{prefs.showPerformance=performance.checked;savePrefs();});document.getElementById('reset-dashboard-settings').addEventListener('click',()=>{Object.assign(prefs,{radarFilter:'all',density:'comfortable',showTimeline:true,showPerformance:true});radar.value=prefs.radarFilter;density.value=prefs.density;timeline.checked=prefs.showTimeline;performance.checked=prefs.showPerformance;savePrefs();if(window.jarvisRenderData)render(window.jarvisRenderData);});savePrefs();}
document.addEventListener('DOMContentLoaded',bindPrefs);
function elapsed(from,to){if(!from||!to)return '—';const ms=new Date(to)-new Date(from);if(!Number.isFinite(ms)||ms<0)return '—';const hours=Math.floor(ms/3600000);return hours<24?hours+'h':Math.floor(hours/24)+'d '+Math.floor(hours%24)+'h';}function render(d){window.jarvisRenderData=d;const t=d.paper_tracker||{},l=d.lifetime||{},all=Object.values(t),buySignals=d.buy_signals||[],activeBuys=buySignals.filter(x=>x.status==="active"),verified=all.filter(x=>x.price_validation_status!=="unverified"),live=all.filter(x=>x.currently_surfaced!==false),avg=verified.length?verified.reduce((s,x)=>s+n(x.return_pct),0)/verified.length:0;
const fmtTime=d.last_scan_completed_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(d.last_scan_completed_at)):'No completed scan timestamp yet';document.getElementById('updated').textContent='Last scan completed: '+fmtTime;

const stageRank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1,'PRICE UNVERIFIED — WATCH ONLY':0};
const surfaced=all.filter(x=>x.currently_surfaced!==false);
const earlyCount=surfaced.filter(x=>['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(x.last_stage)).length;const actionableCount=surfaced.filter(x=>x.last_stage==='ACTIONABLE SETUP').length;
const verifiedCount=surfaced.filter(x=>x.price_validation_status!=='unverified').length;
document.getElementById('radar-stats').innerHTML=[[''+activeBuys.length,'ACTIVE BUY SIGNALS'],[''+surfaced.length,'ON RADAR'],[''+earlyCount,'EARLY / DEVELOPING'],[''+actionableCount,'ACTIONABLE SETUPS'],[''+verifiedCount,'PRICE VERIFIED']].map(x=>'<div class="radar-stat"><strong>'+e(x[0])+'</strong><span>'+x[1]+'</span></div>').join('');
document.getElementById('buy-summary').textContent=activeBuys.length+' active · '+buySignals.length+' total logged';
const recentBuys=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,4);
document.getElementById('buy-signals').innerHTML=recentBuys.length?recentBuys.map(x=>{const ret=x.latest_return_pct==null?'Awaiting update':pct(x.latest_return_pct);const reason=(x.reasons||[]).slice(0,2).join(' · ');return '<article class="buy-card"><div class="buy-card-top"><div><div class="buy-tag">'+(x.status==='active'?'BUY · ACTIVE':'BUY · HISTORICAL')+'</div><div class="buy-symbol">'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</div></div><div class="buy-return '+cl(x.latest_return_pct)+'">'+e(ret)+'</div></div><div class="buy-detail">Signal price: '+e(x.entry_price)+' · Score: '+e(x.score)+'/100<br>Entry zone: '+e((x.entry_low??'—')+' – '+(x.entry_high??'—'))+'<br>Stop-loss: '+e(x.stop_loss??x.invalidation??'—')+' ('+e(x.risk_pct==null?'—':x.risk_pct+'% risk')+')<br>Potential sell: '+e(x.target_1??'—')+' ('+e(x.target_1_pct==null?'—':'+'+x.target_1_pct+'%')+') · Stretch target: '+e(x.target_2??'—')+' ('+e(x.target_2_pct==null?'—':'+'+x.target_2_pct+'%')+')<br>Reward/risk: '+e(x.reward_risk_ratio==null?'—':x.reward_risk_ratio+':1')+'</div><div class="buy-meta">'+date(x.at)+' · '+e(reason)+'</div></article>'}).join(''):'<div class="op-empty">No BUY signals recorded yet. Jarvis will keep watching rather than manufacture a trade.</div>';
const radarPool=surfaced.filter(x=>prefs.radarFilter==='all'||(prefs.radarFilter==='developing'&&['BUY','ACTIONABLE SETUP','DEVELOPING RUNNER','DEVELOPING SETUP','EARLY SETUP'].includes(x.last_stage))||(prefs.radarFilter==='actionable'&&['BUY','ACTIONABLE SETUP'].includes(x.last_stage)));const candidates=radarPool.sort((a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)||n(b.daily_change_pct)-n(a.daily_change_pct)).slice(0,9);
document.getElementById('opportunities').innerHTML=candidates.length?candidates.map((x,i)=>{
 const stage=x.last_stage||'WATCHLIST',s=String(stage),kind=s.includes('ACTIONABLE')?'action':s.includes('LATE RUNNER')?'late':'';
 const reason=(x.stage_reasons||x.discovery_reasons||[]).slice(0,2).join(' · ')||'Candidate retained for continued monitoring.';
 const risks=(x.risk_flags||[]).slice(0,2).join(' · ');
 const price=x.latest_price==null?'—':(Number.isFinite(Number(x.latest_price))?Number(x.latest_price).toPrecision(7):'—');
 const day=x.daily_change_pct==null?'—':pct(x.daily_change_pct);
 const ret=x.price_validation_status==='unverified'?'Unverified':pct(x.return_pct);
 const lv=x.levels||{}, verified=x.price_validation_status!=='unverified'&&(x.asset_type!=='stock'||x.price_validation_status==='verified');
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
 const quality=verified?Math.max(0,Math.min(100,Math.round(Object.values(qualityParts).reduce((a,b)=>a+b,0)-riskPenalty-latePenalty-regimePenalty))):null;
 const confidence=quality;
 const confidenceLabel=confidence===null?'Unavailable':confidence>=80?'Strong':confidence>=65?'Good':confidence>=50?'Developing':'Weak';
 const qualityBreakdown=quality===null?'Price verification required':(stock?'Momentum/trend '+qualityParts.momentum_trend+'/20':'Momentum '+qualityParts.momentum_trend+'/20')+' · Volume '+qualityParts.volume+'/15 · Breakout '+qualityParts.breakout+'/15 · '+(stock?'Technical':'Trend')+' '+qualityParts.technical_confirmation+'/25 · Risk/reward '+qualityParts.risk_reward+'/15 · Agreement '+qualityParts.signal_agreement+'/10';
 const fmtLevel=v=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toPrecision(7);
 const entry=lv.watch_entry_low!=null?fmtLevel(lv.watch_entry_low)+' – '+fmtLevel(lv.watch_entry_high):'Not available';
 const plan=verified&&lv.target_1!=null&&lv.target_2!=null;
 const targetLine=plan?'<div class="op-plan-grid"><div class="op-plan-cell"><strong>'+e(entry)+'</strong><span>POTENTIAL ENTRY RANGE</span></div><div class="op-plan-cell"><strong>'+e(fmtLevel(lv.stop_loss||lv.invalidation_level))+'</strong><span>STOP / INVALIDATION</span></div></div>':'<div class="op-back-note">'+(verified?'No saved ATR/support-based price plan is available for this candidate yet.':'Price is not independently verified. Jarvis is withholding price targets and a quality rating.')+'</div>';
 const calc=lv.calculation||{},riskPer=calc.risk_per_unit!=null?Number(calc.risk_per_unit):((Number(lv.watch_entry_high)||0)-(Number(lv.stop_loss||lv.invalidation_level)||0));
 const entryRef=Number(lv.watch_entry_high)||0,stopRef=Number(lv.stop_loss||lv.invalidation_level)||0;
 const fmtOrDash=v=>v==null||!Number.isFinite(Number(v))?'—':fmtLevel(v);
 const calcLine=plan?'<section class="target-math"><div class="target-math-head"><b>TARGET MATH</b><span>2R / 3R MODEL</span></div><div class="target-math-inputs"><div class="target-math-input"><span>ATR · 14 PERIOD</span><strong>'+e(calc.atr_14==null?'Not saved':fmtOrDash(calc.atr_14))+'</strong></div><div class="target-math-input"><span>RISK UNIT</span><strong>'+e(fmtOrDash(calc.risk_unit))+'</strong></div><div class="target-math-input"><span>ENTRY REFERENCE · HIGH</span><strong>'+e(fmtOrDash(entryRef))+'</strong></div><div class="target-math-input"><span>STOP · '+e((calc.stop_source||'SAVED STOP').toUpperCase())+'</span><strong>'+e(fmtOrDash(stopRef))+'</strong></div></div><div class="target-math-risk"><span>RISK PER SHARE / COIN<br>Entry reference − stop</span><strong>'+e(fmtOrDash(riskPer))+'</strong></div><div class="target-math-level"><div><small>LEVEL 1 · 2R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (2 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_1))+'</strong><span>PRICE TARGET</span></div></div><div class="target-math-level level-two"><div><small>LEVEL 2 · 3R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (3 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_2))+'</strong><span>PRICE TARGET</span></div></div><div class="target-math-foot"><b>How inputs are set:</b> Risk unit = greater of ATR(14) or 2% of signal price. Entry reference = upper entry range. Initial stop distance = 1.5 × risk unit, subject to a 10% price cap and a valid nearby-support adjustment. '+(calc.atr_14==null?'ATR inputs were not retained for this older saved plan.':'')+'</div></section>':'';



 const confidenceWhy=confidence===null?'Independent price verification is required before rating this setup.':qualityBreakdown+'. Deductions: '+(riskPenalty+latePenalty+regimePenalty)+' points for risk flags, late-stage extension and/or risk-off conditions. Setup Quality measures how strongly the available signals support the setup; it is not the probability of making a profit.';
 const evidenceItems=[['MOMENTUM',qualityParts.momentum_trend,20],['VOLUME',qualityParts.volume,15],['BREAKOUT',qualityParts.breakout,15],[stock?'TECHNICAL':'TREND',qualityParts.technical_confirmation,25],['RISK / REWARD',qualityParts.risk_reward,15],['AGREEMENT',qualityParts.signal_agreement,10]];
 const evidenceHtml=evidenceItems.map(([label,score,max])=>'<div class="op-evidence-item"><div><span>'+label+'</span><b>'+score+'/'+max+'</b></div><i><em style="width:'+Math.max(0,Math.min(100,score/max*100))+'%"></em></i></div>').join('');
 const frontQuality=confidence===null?'<div class="op-front-quality unavailable"><div><span>SETUP QUALITY</span><strong>Unavailable</strong></div><small>Awaiting independent price verification</small></div>':'<div class="op-front-quality"><div class="op-front-quality-top"><div><span>SETUP QUALITY · EVIDENCE SCORE</span><strong>'+confidence+'<small>/100</small></strong></div><span class="op-quality-label">'+e(confidenceLabel.toUpperCase())+'</span></div><div class="op-quality-track"><i style="width:'+confidence+'%"></i></div><div class="op-evidence-grid">'+evidenceHtml+'</div></div>';
 const frontPlan=plan?'<div class="op-front-plan"><div><span>ENTRY ZONE</span><b>'+e(entry)+'</b></div><div><span>STOP / INVALIDATION</span><b>'+e(fmtLevel(lv.stop_loss||lv.invalidation_level))+'</b></div></div>':(verified?'':'<div class="op-front-plan-note">Price unverified · trade levels withheld</div>');
 const frontExplanation='<div class="op-front-explanation"><div>'+e(confidenceWhy)+'</div><div>Method: '+e(lv.method||'ATR-based stop and 2R / 3R reward-to-risk targets where saved.')+'</div></div>';

 return '<article class="op-card op-flippable '+(i===0?'featured':'')+'" tabindex="0" role="button" aria-pressed="false" aria-label="Flip '+e(x.symbol||'candidate')+' opportunity card for targets and confidence"><div class="op-flip-inner"><div class="op-face op-face-front"><div class="op-card-top"><div><div class="op-kind">'+(i===0?'TOP RADAR CANDIDATE · ':'')+e((x.asset_type||'').toUpperCase())+'</div><div class="op-symbol">'+e(x.symbol||'—')+'</div></div><span class="op-stage '+kind+'">'+e(stage)+'</span></div><div class="op-metrics"><div class="op-metric"><strong>'+e(price)+'</strong><span>LATEST PRICE</span></div><div class="op-metric"><strong class="'+cl(x.daily_change_pct)+'">'+e(day)+'</strong><span>24H MOVE</span></div><div class="op-metric"><strong class="'+cl(x.return_pct)+'">'+e(ret)+'</strong><span>RETURN SINCE FIRST VERIFIED</span></div></div>'+frontQuality+frontExplanation+frontPlan+'<div class="op-reason"><b>WHY IT&#39;S ON RADAR</b><br>'+e(reason)+'</div>'+(risks?'<div class="op-risk">RISK FLAGS: '+e(risks)+'</div>':'')+'<div class="op-flip-hint">TAP FOR TARGETS, STOP LOGIC + FULL SCORE BREAKDOWN ↻</div></div><div class="op-face op-face-back"><div class="op-back-title">'+e((x.symbol||'CANDIDATE')+' · POTENTIAL TRADE PLAN')+'</div>'+targetLine+calcLine+'<div class="op-flip-hint">TAP TO RETURN TO OPPORTUNITY ↻</div></div></div></article>';
}).join(''):'<div class="op-empty">Nothing has qualified for the radar yet. Jarvis will keep monitoring and show candidates after a successful scan.</div>';
document.querySelectorAll('.op-flippable').forEach(card=>{const flip=()=>{const on=card.classList.toggle('is-flipped');card.setAttribute('aria-pressed',String(on));};card.addEventListener('click',flip);card.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();flip();}});});
const timelineItems=surfaced.slice().sort((a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)).slice(0,12);document.getElementById('radar-timeline-cards').innerHTML=timelineItems.length?timelineItems.map(x=>{const history=(x.grade_history||[]).slice(-3).reverse();const events=history.length?history.map(h=>'<div class="timeline-event"><b>'+e(h.to||'Stage update')+'</b> · '+e(date(h.at))+'</div>').join(''):'<div class="timeline-event">First recorded stage: '+e(x.last_stage||'Watching')+'</div>';return '<article class="timeline-card"><div class="timeline-top"><div class="timeline-symbol">'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</div><span class="timeline-stage">'+e(x.last_stage||'WATCHING')+'</span></div><div class="timeline-events">'+events+'</div></article>';}).join(''):'<div class="op-empty">No candidates are currently on the radar.</div>';
const observed=buySignals.filter(x=>x.target_1||x.target_2||x.stop_loss);const l1=observed.filter(x=>x.level_observations&&x.level_observations.target_1_observed),l2=observed.filter(x=>x.level_observations&&x.level_observations.target_2_observed),stops=observed.filter(x=>x.level_observations&&x.level_observations.stop_observed);document.getElementById('target-performance').innerHTML=[['BUY plans tracked',observed.length],['Level 1 observed',l1.length+' / '+observed.length],['Level 2 observed',l2.length+' / '+observed.length],['Stop observed',stops.length+' / '+observed.length]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');document.getElementById('target-tracking').innerHTML=observed.slice().sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,40).map(x=>{const obs=x.level_observations||{};return '<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e(x.entry_price??'—')+'</td><td>'+e(x.target_1??'—')+(obs.target_1_observed?'<div class="tiny pos">Observed '+e(date(obs.target_1_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.target_2??'—')+(obs.target_2_observed?'<div class="tiny pos">Observed '+e(date(obs.target_2_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.stop_loss??'—')+(obs.stop_observed?'<div class="tiny neg">Observed '+e(date(obs.stop_observed.at))+'</div>':'')+'</td><td>'+e(obs.target_1_observed?elapsed(x.at,obs.target_1_observed.at):'—')+'</td><td>'+e(obs.target_2_observed?elapsed(x.at,obs.target_2_observed.at):'—')+'</td></tr>';}).join('')||'<tr><td colspan="7" class="empty">BUY target observations will appear as Jarvis records new scans.</td></tr>';

const thresholds=[['Positive now',verified.filter(x=>n(x.return_pct)>0).length],['At least +5%',verified.filter(x=>n(x.return_pct)>=5).length],['At least +10%',verified.filter(x=>n(x.return_pct)>=10).length],['At least +20%',verified.filter(x=>n(x.return_pct)>=20).length],['Down 5% or more',verified.filter(x=>n(x.return_pct)<=-5).length],['Ever reached +10%',verified.filter(x=>n(x.max_gain_pct)>=10).length]];
document.getElementById('stats').innerHTML=[['Tracked candidates',all.length],['In latest scan',live.length],['Average return',pct(avg)],['Positive candidates',thresholds[0][1]+' / '+verified.length],['Lifetime checkpoint wins',Object.values(l.checkpoints||{}).reduce((s,x)=>s+n(x.wins),0)],['Top current return',pct(Math.max(0,...verified.map(x=>n(x.return_pct))))]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');
document.getElementById('assets').innerHTML=['stock','crypto'].map(k=>{const a=verified.filter(x=>x.asset_type===k),m=a.length?a.reduce((s,x)=>s+n(x.return_pct),0)/a.length:0;return '<button type="button" class="panel asset-jump" data-asset="'+k+'" aria-label="Show '+(k==='stock'?'stock':'crypto')+' candidates"><div class="muted">'+(k==='stock'?'Stocks':'Crypto')+'</div><div class="value">'+a.length+' tracked</div><div class="'+cl(m)+'">Average return '+pct(m)+'</div><div class="tiny">'+a.filter(x=>n(x.return_pct)>0).length+' positive · '+a.filter(x=>x.last_stage==='ACTIONABLE SETUP').length+' actionable grade</div><div class="tiny" style="margin-top:8px;color:var(--accent)">View '+(k==='stock'?'stock':'crypto')+' list →</div></button>'}).join('');
document.getElementById('buy-history').innerHTML=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'<div class="tiny">'+e(x.status||'')+'</div></td><td>'+date(x.at)+'</td><td>'+e(x.entry_price)+'</td><td class="'+cl(x.latest_return_pct)+'">'+(x.latest_return_pct==null?'—':pct(x.latest_return_pct))+'</td>'+['1h','4h','1d','3d','7d'].map(h=>'<td class="'+cl(x.outcomes&&x.outcomes[h]&&x.outcomes[h].return_pct)+'">'+(x.outcomes&&x.outcomes[h]?pct(x.outcomes[h].return_pct):'—')+'</td>').join('')+'<td>'+e(x.score)+'/100</td></tr>').join('')||'<tr><td colspan="10" class="empty">No BUY recommendations have been recorded yet.</td></tr>';
const byReturn=[...verified].sort((a,b)=>n(b.return_pct)-n(a.return_pct));document.getElementById('leaders').innerHTML=byReturn.slice(0,10).map((x,i)=>'<tr><td>'+(i+1)+'</td><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td>'+n(x.latest_price).toPrecision(7)+'</td><td class="'+cl(x.return_pct)+'">'+pct(x.return_pct)+'</td><td class="pos">'+pct(x.max_gain_pct)+'</td><td class="neg">'+pct(x.max_drawdown_pct)+'</td></tr>').join('')||'<tr><td colspan="7" class="empty">No tracked candidates yet.</td></tr>';
function rows(){const q=document.getElementById('q').value.toUpperCase(),a=document.getElementById('assetFilter').value,g=document.getElementById('gradeFilter').value;const v=all.filter(x=>(a==='all'||x.asset_type===a)&&(g==='all'||x.last_stage===g)&&(!q||String(x.symbol).toUpperCase().includes(q)));v.sort((x,y)=>(rank[y.last_stage]||0)-(rank[x.last_stage]||0)||(y.last_seen||'').localeCompare(x.last_seen||''));document.getElementById('rows').innerHTML=v.map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.first_seen)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td><span class="pill">'+e(x.last_stage||'—')+'</span></td><td>'+e(x.price_source||'Legacy record — source not recorded')+'<div class="tiny">'+e(x.price_freshness||'Freshness unknown')+(x.price_validation_status==='unverified'?' · PRICE UNVERIFIED — excluded from returns':'')+'</div></td><td>'+date(x.price_as_of||x.last_seen)+'</td><td class="'+cl(x.session_change_pct)+'">'+(x.price_validation_status==='unverified'||x.session_change_pct==null?'—':pct(x.session_change_pct))+'</td><td class="'+cl(x.return_pct)+'">'+(x.price_validation_status==='unverified'?'UNVERIFIED':pct(x.return_pct))+'</td><td class="pos">'+(x.price_validation_status==='unverified'?'—':pct(x.max_gain_pct))+'</td><td class="neg">'+(x.price_validation_status==='unverified'?'—':pct(x.max_drawdown_pct))+'</td><td>'+n(x.age_days).toFixed(1)+'d</td></tr>').join('')||'<tr><td colspan="11" class="empty">No matches.</td></tr>'}
['q','assetFilter','gradeFilter'].forEach(id=>document.getElementById(id).addEventListener(id==='q'?'input':'change',rows));document.querySelectorAll('.asset-jump').forEach(card=>card.addEventListener('click',()=>{document.getElementById('assetFilter').value=card.dataset.asset;document.getElementById('gradeFilter').value='all';document.getElementById('q').value='';rows();document.getElementById('candidate-ledger').scrollIntoView({behavior:'smooth',block:'start'});}));rows();
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

    # GitHub Actions commits these tracker files after each scan. Read the
    # committed files as the single source of truth; never prefer a Render-local
    # snapshot, which can be stale or disappear during a redeploy.
    paper = read_state(PAPER_STATE)
    lifetime = read_state(PAPER_LIFETIME_STATE)
    buy_state = read_state(BUY_SIGNAL_STATE)
    # Read the completion timestamp written by the GitHub Actions scan workflow.
    # Never infer scan time from file mtimes or from a page refresh.
    scan_metadata = read_state(Path(__file__).resolve().parent / "scan_metadata.json")
    return {
        "last_scan_completed_at": scan_metadata.get("last_scan_completed_at"),
        "scan_run_id": scan_metadata.get("run_id"),
        "paper_tracker": paper,
        "lifetime": lifetime,
        "buy_signals": buy_state.get("signals", []),
    }


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
    scan_started = time.monotonic()
    stock_symbols = stock_universe() if asset in ("all", "stocks") else []
    crypto_symbols = crypto_universe() if asset in ("all", "crypto") else []
    stock_results, stock_errors = ([], [])
    crypto_results, crypto_errors = ([], [])

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
                "stages": {stage: sum(1 for x in stock_results if x.get("stage") == stage) for stage in ("BUY", "ACTIONABLE SETUP", "DEVELOPING RUNNER", "DEVELOPING SETUP", "EARLY SETUP", "EARLY WATCH", "LATE RUNNER — CHASE RISK", "LOW-CONVICTION WATCH", "PRICE UNVERIFIED — WATCH ONLY")},
            },
            "crypto": {
                "symbols_requested": len(crypto_symbols),
                "candidates_returned": len(crypto_results),
                "scan_errors": len(crypto_errors),
                "not_returned_or_below_early_watch_floor": max(0, len(crypto_symbols) - len(crypto_results) - len(crypto_errors)),
                "stages": {stage: sum(1 for x in crypto_results if x.get("stage") == stage) for stage in ("BUY", "ACTIONABLE SETUP", "DEVELOPING SETUP", "EARLY WATCH", "LOW-CONVICTION WATCH")},
            },
            "provider_note": "Counts show requested symbols and scan outcomes. A symbol not returned may have insufficient history/data or score below the early-watch floor; provider-specific discovery failures are not always attributable to one symbol.",
            "webull_quote_status": "enabled" if WEBULL_QUOTE_ENABLED else "disabled_pending_authorization",
            "webull_quote_note": "Webull quote lookups are opt-in because app authorization was observed pending; Yahoo/Stooq scanning continues independently. Enable only after Webull market-data authorization is confirmed.",
            "massive_live_quote_status": "enabled" if MASSIVE_LIVE_QUOTE_ENABLED else "disabled_pending_entitlement_check",
            "massive_live_quote_note": "Optional last-trade cross-check is disabled by default. Enable MASSIVE_LIVE_QUOTE_ENABLED=true only if the Massive Stocks plan includes /v2/last/trade; 15-minute delayed data is accepted only when its timestamp is within 45 minutes.",
            "market_session": market_session_status(),
            "scan_duration_seconds": round(time.monotonic() - scan_started, 2),
        },
    }


@app.get("/research", response_class=HTMLResponse, dependencies=[Depends(require_site_login)])
def research_page():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis Research Lab</title><style>
:root{color-scheme:dark;--bg:#0b1119;--surface:#121e2b;--surface2:#172638;--line:#293b50;--muted:#9aabbd;--text:#edf3fa;--accent:#83b8ff;--green:#63d9a0;--amber:#f0c477}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#1a2b40 0%,var(--bg) 58%);color:var(--text);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{width:min(100% - 32px,1320px);margin:auto;padding:28px 0 55px}
a{color:var(--accent);text-decoration:none}.muted,.sub{color:var(--muted);font-size:13px}.eyebrow{font-size:10px;letter-spacing:.15em;color:var(--accent);font-weight:750}h1{font-size:clamp(28px,4vw,38px);margin:5px 0}h2{font-size:19px;margin:0 0 6px}
.tabs{display:flex;gap:8px;margin:20px 0;border-bottom:1px solid var(--line);padding-bottom:10px}.tabs a{padding:8px 14px;border:1px solid var(--line);border-radius:10px;color:var(--muted)}.tabs .active{background:#20344a;color:var(--text);border-color:#47617d}
.hero,.metric,.feature{background:linear-gradient(145deg,var(--surface2),var(--surface));border:1px solid var(--line);border-radius:14px;padding:16px}.hero{display:grid;grid-template-columns:1.6fr 1fr;gap:18px}.hero p{color:var(--muted);margin:8px 0 0}.notice{border-left:3px solid var(--amber);padding:10px;background:#f0c47712;border-radius:7px;color:#e8d7b4;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin:15px 0 25px}.metric .k{font-size:10px;color:var(--muted);letter-spacing:.08em}.metric strong{display:block;font-size:25px;margin-top:5px}
.section{margin-top:26px}.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:10px;margin-top:12px}.feature b{font-size:13px}.feature p{font-size:12px;color:var(--muted);margin:5px 0}.status{font-size:10px;color:var(--amber);text-transform:uppercase;letter-spacing:.08em}.status.partial{color:var(--green)}
.scroll{overflow:auto;border:1px solid var(--line);border-radius:12px;margin-top:12px}table{width:100%;border-collapse:collapse;min-width:780px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #26384a;font-size:12px;vertical-align:top}th{background:#172638;color:#b9c9da;font-size:10px;text-transform:uppercase;letter-spacing:.06em}tbody tr:nth-child(even){background:#ffffff06}.pill{padding:3px 7px;border-radius:999px;background:#293b50;font-size:10px}.empty{text-align:center;padding:20px;color:var(--muted)}.foot{font-size:11px;color:var(--muted);margin-top:22px}
@media(max-width:700px){main{width:calc(100% - 24px);padding-top:20px}.hero{grid-template-columns:1fr}.metric{padding:12px}.metric strong{font-size:21px}}
</style></head><body><main><div class="eyebrow">JARVIS · MARKET INTELLIGENCE</div><h1>Research lab</h1><div class="sub" id="updated">Loading saved research records…</div>
<nav class="tabs"><a href="/history">Overview</a><a class="active" href="/research">Research</a></nav>
<section class="hero"><div><div class="eyebrow">EXPERIMENTAL SIGNALS · NOT TRADE TRIGGERS</div><h2>Measure first. Promote only what earns it.</h2><p>This workspace evaluates additional momentum-entry signals alongside the existing scanner. It does not change BUY scoring, place trades, or treat unverified quotes as valid results.</p></div><div class="notice"><b>Research-only mode</b><br>Existing data is shown separately from measurements that still need to be added. Missing values stay blank rather than being guessed.<div id="sessionStatus" style="margin-top:9px;color:#c5d6e8">Checking market session and quote providers…</div></div></section>
<div class="grid" id="stats"></div>
<section class="section"><h2>Professional signal checklist</h2><div class="muted">A transparent status for each proposed research feature.</div><div class="features" id="features"></div></section>
<section class="section"><h2>BUY opportunities &amp; research discoveries</h2><div class="muted">Potential plans are hypothetical reference levels from Jarvis. Stops and targets are not guaranteed fills. Existing BUY engine signals are kept separate from experimental research matches.</div>
<div style="margin-top:16px"><h3 style="font-size:14px;margin:0 0 5px">Current Jarvis BUY signals</h3><div class="muted">Official scanner BUY-grade records with their saved entry, stop and target levels.</div><div class="scroll"><table style="min-width:900px"><thead><tr><th>Candidate</th><th>Origin</th><th>Trigger / reason</th><th>Entry range</th><th>Stop loss</th><th>Target 1</th><th>Target 2</th><th>R:R</th><th>Status</th></tr></thead><tbody id="buyLedger"><tr><td colspan="9" class="empty">Loading BUY records…</td></tr></tbody></table></div></div>
<div style="margin-top:18px"><h3 style="font-size:14px;margin:0 0 5px">Research-layer discoveries</h3><div class="muted">Candidates explicitly matched by experimental research signals. They are not promoted to official BUYs by this tab.</div><div class="scroll"><table style="min-width:900px"><thead><tr><th>Candidate</th><th>Research signals matched</th><th>Jarvis grade</th><th>Entry range</th><th>Stop loss</th><th>Target 1</th><th>Target 2</th><th>Research status</th></tr></thead><tbody id="researchBuyLedger"><tr><td colspan="8" class="empty">Loading research matches…</td></tr></tbody></table></div></div>
</section>
<section class="section"><h2>Candidate research ledger</h2><div class="muted">Latest saved records; metrics appear only after the scanner actually records them.</div><div class="scroll"><table><thead><tr><th>Asset</th><th>Grade</th><th>Price validation</th><th>Relative strength</th><th>Same-time RVOL</th><th>VWAP / opening range</th><th>ADX / DMI</th><th>Liquidity / spread</th><th>Catalyst / dilution</th></tr></thead><tbody id="ledger"><tr><td colspan="9" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="section"><h2>How we will judge the signals</h2><div class="features">
<div class="feature"><b>Forward returns</b><p>1h, 4h, 24h, 72h, 5-day and 10-day observations where verified timestamps and prices exist.</p><span class="status">To be instrumented</span></div>
<div class="feature"><b>Favorable / adverse excursion</b><p>Measure the best upside and worst pullback after each signal, not only the final return.</p><span class="status">To be instrumented</span></div>
<div class="feature"><b>False-breakout rate</b><p>Record failed breaks, retests, spread and slippage assumptions.</p><span class="status">To be instrumented</span></div>
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
const stats=[["Tracked candidates",all.length],["Stocks",all.filter(x=>x.asset_type==="stock").length],["Crypto",all.filter(x=>x.asset_type==="crypto").length],["Price verified",verified.length],["Currently surfaced",all.filter(x=>x.currently_surfaced!==false).length]];
document.getElementById("stats").innerHTML=stats.map(x=>'<div class="metric"><div class="k">'+esc(x[0])+'</div><strong>'+esc(x[1])+'</strong></div>').join("");
const features=[
["Cross-sectional relative strength","Compare symbols with SPY, IWM, sector ETFs and peers.","To be instrumented"],
["Same-time-of-day relative volume","Compare each intraday bar with the same time window across prior sessions.","To be instrumented"],
["Liquidity and spread","Dollar volume, bid/ask spread and practical execution-risk checks.","To be instrumented"],
["Opening range and retest","5/15/30-minute range, breakout, retest and failure structure.","To be instrumented"],
["Anchored VWAP","VWAP anchored to catalyst, gap, swing low or breakout event.","To be instrumented"],
["ADX / DMI","Trend strength and direction; identify weak or choppy conditions.","To be instrumented"],
["Volatility contraction","Base tightness, ATR contraction and expansion after compression.","To be instrumented"],
["Catalyst and dilution","News/filings, shelf/offering/warrant and float-risk classification.","Partially present"]
];
document.getElementById("features").innerHTML=features.map(x=>'<div class="feature"><b>'+esc(x[0])+'</b><p>'+esc(x[1])+'</p><span class="status '+(x[2].startsWith("Partially")?"partial":"")+'">'+esc(x[2])+'</span></div>').join("");
const money=v=>{const n=Number(v);return Number.isFinite(n)&&n>0?n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:6}):"—"};const arr=v=>Array.isArray(v)?v.filter(Boolean).map(esc).join(" · "):esc(v||"—");
const level=(o,k)=>money(o&&o[k]);
const buys=(d.buy_signals||[]).filter(x=>x.status==="active").sort((a,b)=>(b.at||"").localeCompare(a.at||""));
document.getElementById("buyLedger").innerHTML=buys.length?buys.map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b><br><span class="muted">'+esc(x.at||"")+'</span></td><td><span class="pill">'+esc(x.discovery_source==="research_layer"?"Research-tagged":"Core BUY engine")+'</span></td><td>'+arr(x.research_signal_matches&&x.research_signal_matches.length?x.research_signal_matches:x.reasons)+'</td><td>'+money(x.entry_low)+' – '+money(x.entry_high)+'</td><td class="amber">'+level(x,"stop_loss")+'</td><td class="green">'+level(x,"target_1")+'</td><td class="green">'+level(x,"target_2")+'</td><td>'+esc(x.reward_risk_ratio?x.reward_risk_ratio+":1":"—")+'</td><td>'+esc(x.status||"active")+'</td></tr>').join(""):'<tr><td colspan="9" class="empty">No active official BUY signals are recorded right now. This is not a statement that the market has no opportunities.</td></tr>';
const researchBuys=all.filter(x=>Array.isArray(x.research_signal_matches)&&x.research_signal_matches.length).sort((a,b)=>(b.last_seen||"").localeCompare(a.last_seen||""));
document.getElementById("researchBuyLedger").innerHTML=researchBuys.length?researchBuys.map(x=>{const lv=x.levels||{};return '<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b></td><td>'+arr(x.research_signal_matches)+'</td><td><span class="pill">'+esc(x.last_stage||"—")+'</span></td><td>'+money(lv.watch_entry_low)+' – '+money(lv.watch_entry_high)+'</td><td class="amber">'+money(lv.stop_loss||lv.invalidation_level)+'</td><td class="green">'+money(lv.target_1)+'</td><td class="green">'+money(lv.target_2)+'</td><td>'+esc(x.research_setup_status||"Observed match · not a trade trigger")+'</td></tr>';}).join(""):'<tr><td colspan="8" class="empty">No research-layer matches recorded yet. The additional indicators still need to be wired into candidate discovery; Jarvis will not invent signal attribution.</td></tr>';document.getElementById("ledger").innerHTML=all.length?all.sort((a,b)=>(b.last_seen||"").localeCompare(a.last_seen||"")).slice(0,100).map(x=>'<tr><td><b>'+esc((x.asset_type||"").toUpperCase()+" "+x.symbol)+'</b></td><td><span class="pill">'+esc(x.last_stage||"—")+'</span></td><td>'+esc(x.price_validation_status||"unknown")+'<br><span class="muted">'+esc(x.price_source||"source not recorded")+'</span><br><span class="muted">'+esc(x.price_validation_note||"Validation detail not recorded")+'</span><br><span class="muted">Massive: '+esc(x.massive_quote_status||"not reported")+'</span></td><td>'+pick(x,["relative_strength_vs_spy","relative_strength_pct","rs_rank"])+'</td><td>'+pick(x,["intraday_rvol_same_time","same_time_relative_volume"])+'</td><td>'+pick(x,["anchored_vwap_status","opening_range_status","vwap_reclaim"])+'</td><td>'+pick(x,["adx","adx_dmi_status"])+'</td><td>'+pick(x,["spread_pct","dollar_volume","liquidity_status"])+'</td><td>'+researchDetail(x.catalyst_dilution_status??x.dilution_flags??x.catalysts)+'</td></tr>').join(""):'<tr><td colspan="9" class="empty">No candidate records available.</td></tr>';}
fetch("/research/data").then(r=>{if(!r.ok)throw Error();return r.json()}).then(render).catch(()=>{document.getElementById("updated").textContent="Research data unavailable";document.getElementById("ledger").innerHTML='<tr><td colspan="9" class="empty">Could not load saved research data. Try again after the next successful sync.</td></tr>';});
</script></body></html>""")


@app.get("/research/data", dependencies=[Depends(require_site_login)])
def research_data():
    def read_state(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    paper = read_state(PAPER_STATE)
    lifetime = read_state(PAPER_LIFETIME_STATE)
    buy_state = read_state(BUY_SIGNAL_STATE)
    records = list(paper.values()) if isinstance(paper, dict) else []
    fields = ("relative_strength_vs_spy", "relative_strength_pct", "rs_rank",
              "intraday_rvol_same_time", "same_time_relative_volume",
              "anchored_vwap_status", "opening_range_status", "adx", "adx_dmi_status",
              "spread_pct", "dollar_volume", "liquidity_status",
              "catalyst_dilution_status", "dilution_flags")
    massive_key = bool(os.getenv("MASSIVE_API_KEY", "").strip())
    webull_keys = bool(os.getenv("WEBULL_APP_KEY", "").strip() and os.getenv("WEBULL_APP_SECRET", "").strip())
    return {"synced_at": now_iso(), "paper_tracker": paper, "lifetime": lifetime,
            "buy_signals": buy_state.get("signals", []) if isinstance(buy_state, dict) else [],
            "market_session": market_session_status(),
            "quote_provider_status": {
                "massive_live_quotes": ("Massive live quotes: enabled" if MASSIVE_LIVE_QUOTE_ENABLED and massive_key else
                                        "Massive live quotes: disabled by safety setting" if not MASSIVE_LIVE_QUOTE_ENABLED else
                                        "Massive live quotes: API key missing"),
                "webull": ("Webull: enabled" if WEBULL_QUOTE_ENABLED and webull_keys else
                           "Webull: authorization pending / provider disabled" if webull_keys else
                           "Webull: credentials not configured"),
            },
            "research_field_coverage": {key: sum(1 for r in records if isinstance(r, dict) and r.get(key) is not None) for key in fields}}