import os
import json
import hmac
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
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
        late_signals = []
        if float(item.get("daily_change_pct") or 0) >= 10: late_signals.append("daily move at least 10%")
        if float(item.get("five_day_change_pct") or 0) >= 25: late_signals.append("five-day move at least 25%")
        if float(item.get("intraday_change_pct") or 0) >= 8: late_signals.append("intraday move at least 8%")
        if rsi is not None and float(rsi) >= 78: late_signals.append("RSI at least 78")
        if extension_atr >= 2.5: late_signals.append("price at least 2.5 ATR above EMA20")

        early_signals = []
        if float(item.get("relative_volume") or 0) >= 1.3: early_signals.append("daily relative volume building")
        if float(item.get("intraday_relative_volume") or 0) >= 1.5: early_signals.append("intraday relative volume building")
        if tech.get("bollinger_squeeze"): early_signals.append("Bollinger squeeze")
        if tech.get("obv_trend_up_10d"): early_signals.append("OBV trend improving")
        if item.get("above_session_vwap"): early_signals.append("holding above session VWAP")
        if tech.get("ema_9_above_ema_20"): early_signals.append("short-term EMA trend positive")

        technical_confirmations = [
            bool(tech.get("ema_9_above_ema_20") and item.get("above_20d_average")),
            bool(safe_float(tech.get("macd_histogram")) > 0 and tech.get("macd_histogram_rising")),
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
    # Illustrative planning levels only; not personalized trade instructions.
    # Use a volatility-aware stop, tightened to nearby support when appropriate.
    # Cap initial risk distance at 10% to avoid unusably distant stop levels.
    risk_unit = max(safe_float(atr), price * 0.02)
    risk_distance = min(1.5 * risk_unit, price * 0.10)
    atr_stop = price - risk_distance
    support_value = safe_float(support)
    support_stop = support_value - 0.15 * risk_unit if 0 < support_value < price else 0.0
    usable_support_stop = support_stop if 0 < support_stop < price and (price - support_stop) <= price * 0.10 else 0.0
    stop_loss = max(atr_stop, usable_support_stop, price * 0.01)
    risk_per_unit = max(0.0, price - stop_loss)
    target_1 = price + 2.0 * risk_per_unit
    target_2 = price + 3.0 * risk_per_unit
    return {
        "watch_entry_low": round(price, 6),
        "watch_entry_high": round(price + risk_unit * 0.25, 6),
        "invalidation_level": round(stop_loss, 6),
        "stop_loss": round(stop_loss, 6),
        "target_1": round(target_1, 6),
        "target_2": round(target_2, 6),
        "risk_pct": round(risk_per_unit / price * 100, 2) if price > 0 else None,
        "target_1_pct": round((target_1 / price - 1) * 100, 2) if price > 0 else None,
        "target_2_pct": round((target_2 / price - 1) * 100, 2) if price > 0 else None,
        "reward_risk_ratio": 2.0,
        "target_2_reward_risk_ratio": 3.0,
        "method": "ATR-based stop, tightened to nearby support when appropriate; targets at 2R and 3R. Reassess if price/volatility changes.",
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
WEBULL_QUOTE_SEMAPHORE = threading.BoundedSemaphore(4)


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

    # SDK handles Webull's request-signature authentication. This client is
    # read-only in Jarvis: only the Market Data API is instantiated/called.
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

    validation_passed = False
    validation_note = "Fresh Yahoo intraday quote unavailable"
    secondary_quote = None
    quote_difference_pct = None
    if intraday_fresh and intraday_price > 0:
        try:
            secondary_quote = stooq_latest_quote(symbol)
            quote_difference_pct = abs(intraday_price - secondary_quote["price"]) / ((intraday_price + secondary_quote["price"]) / 2) * 100
            validation_passed = quote_difference_pct <= 3.0
            validation_note = (f"Yahoo/Stooq agree within {quote_difference_pct:.2f}%" if validation_passed else f"PRICE DISAGREEMENT: Yahoo ${intraday_price:.4f} vs Stooq ${secondary_quote['price']:.4f}")
        except Exception as exc:
            validation_note = f"Independent Stooq validation unavailable: {exc}"
    webull_quote = None
    webull_difference_pct = None
    webull_status = "not_configured"
    if validation_passed and os.getenv("WEBULL_APP_KEY", "").strip() and os.getenv("WEBULL_APP_SECRET", "").strip():
        try:
            webull_quote = webull_latest_bar(symbol)
            webull_difference_pct = abs(intraday_price - webull_quote["price"]) / ((intraday_price + webull_quote["price"]) / 2) * 100
            if webull_difference_pct > 3.0:
                validation_passed = False
                validation_note = f"PRICE DISAGREEMENT: Yahoo/Stooq vs Webull differ by {webull_difference_pct:.2f}%"
                webull_status = "price_mismatch"
            else:
                validation_note += f"; Webull confirms within {webull_difference_pct:.2f}%"
                webull_status = "confirmed"
        except Exception:
            # Webull is additive: missing entitlement, credentials, or outage
            # must never interrupt the existing Yahoo + Stooq scan.
            webull_status = "unavailable_or_not_entitled"

    if validation_passed:
        price = intraday_price
        daily_change = (price / previous - 1) * 100
        five_day_change = (price / safe_float(close.iloc[-6], price) - 1) * 100
    breakout = price > safe_float(close.iloc[-21:-1].max())
    trend_up = price > safe_float(close.tail(20).mean())
    # Score independent evidence groups once; cap secondary confirmations so
    # correlated momentum/volume indicators cannot dominate the ranking.
    score_components = {}
    score_components["daily_momentum"] = 15 if daily_change >= 2 else 8 if daily_change >= 0.5 else 0
    score_components["five_day_momentum"] = 15 if five_day_change >= 5 else 8 if five_day_change >= 2 else 0
    score_components["daily_volume"] = 20 if relative_volume >= 2.5 else 14 if relative_volume >= 1.8 else 7 if relative_volume >= 1.3 else 0
    score_components["breakout"] = 20 if breakout else 0
    score_components["trend"] = 15 if trend_up else 0
    intraday_group = (
        (7 if intraday_change >= 2 else 4 if intraday_change >= 0.75 else 0)
        + (5 if intraday_relative_volume >= 3 else 3 if intraday_relative_volume >= 2 else 0)
        + (3 if intraday_breakout else 0)
    )
    score_components["intraday_confirmation"] = min(15, intraday_group)
    technical_group = (
        (3 if technical.get("ema_9_above_ema_20") and trend_up else 0)
        + (2 if technical.get("macd_histogram", 0) > 0 and technical.get("macd_histogram_rising") else 0)
        + (1 if technical.get("rsi_14") is not None and 50 <= technical["rsi_14"] <= 70 else 0)
        + (2 if vwap_metrics.get("vwap_reclaim") else 0)
        + (2 if technical.get("bollinger_squeeze_expansion") else 0)
        + (1 if technical.get("obv_trend_up_10d") else 0)
    )
    score_components["technical_confirmation"] = min(8, technical_group)
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
        "price_as_of": intraday_as_of.isoformat() if validation_passed else pd.Timestamp(frame.index[-1]).isoformat(),
        "price_freshness": "fresh_15m_cross_checked" if validation_passed else "daily_bar_unverified",
        "price_validation_passed": validation_passed,
        "price_validation_note": validation_note,
        "secondary_price": round(secondary_quote["price"], 4) if secondary_quote else None,
        "price_difference_pct": round(quote_difference_pct, 2) if quote_difference_pct is not None else None,
        "webull_price": round(webull_quote["price"], 4) if webull_quote else None,
        "webull_price_as_of": webull_quote["as_of"] if webull_quote else None,
        "webull_price_age_minutes": webull_quote["age_minutes"] if webull_quote else None,
        "webull_price_difference_pct": round(webull_difference_pct, 2) if webull_difference_pct is not None else None,
        "webull_status": webull_status,
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
        "intraday_confirmation_available": bool(validation_passed),
        "research_notes": [
            "Review latest SEC filings for financing, dilution, debt and material events.",
            "Confirm headline timing and catalyst with a second independent source before acting.",
            "Check float, shares outstanding, cash runway and upcoming earnings; these are not fully verified by this scan.",
        ],
        "data_source": "Yahoo Finance 15m + independent Stooq quote" if validation_passed else f"{price_provider} daily; live quote unverified",
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
.op-reason{font-size:11px;color:#c4d2e0;line-height:1.5;margin-top:8px}.op-risk{font-size:10px;color:#ffb0a6;margin-top:8px}.op-empty{border:1px dashed #40546a;border-radius:12px;padding:20px;color:var(--muted);font-size:13px;grid-column:1/-1}.footnote{margin-top:13px}
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
</style></head><body><main><header class="command-header"><div class="brand-lockup"><div class="brand-mark">J</div><div><div class="eyebrow">MARKET INTELLIGENCE SYSTEM</div><h1>JARVIS<span class="title-dot">.</span></h1><div class="brand-sub">EARLY-MOVE DETECTION · STOCKS + DIGITAL ASSETS</div></div></div><div class="header-right"><div class="live-status" id="updated">Connecting to saved history…</div><div class="tiny">RESEARCH ONLY · NO AUTOMATED TRADES</div></div></header><section class="hero-panel"><div class="hero-copy"><div class="eyebrow">THE WATCHFLOOR</div><h2>Find the move<br><span>before the crowd.</span></h2><p>Jarvis is watching for developing momentum, unusual activity, and early technical shifts. Here’s what has earned a closer look.</p></div><div class="radar-art" aria-hidden="true"><div class="radar-ring ring-one"></div><div class="radar-ring ring-two"></div><div class="radar-ring ring-three"></div><div class="radar-sweep"></div><div class="radar-core"></div><span class="radar-label radar-label-one">MOMENTUM</span><span class="radar-label radar-label-two">VOLUME</span><span class="radar-label radar-label-three">BREAKOUT</span></div></section><section class="buy-floor"><div class="section-heading"><div><div class="eyebrow">STRICT SIGNAL LOG</div><h2>BUY signals</h2><div class="muted">Only setups that pass Jarvis's full entry filter. Paper-tracked; no trades are placed.</div></div><div class="tiny" id="buy-summary">Waiting for saved signal history</div></div><div id="buy-signals" class="buy-grid"><div class="op-empty">No BUY signals recorded yet. Jarvis will only show one when every required condition passes.</div></div></section><section class="radar-section"><div class="section-heading"><div><div class="eyebrow">LIVE WATCHLIST</div><h2>Opportunity radar</h2><div class="muted">Candidates are ranked by setup stage—not just today's biggest gainers.</div></div><div class="tiny">VERIFY PRICE + RISK BEFORE ANY DECISION</div></div><div id="radar-stats" class="radar-stats"><div class="radar-stat"><strong>—</strong><span>ON RADAR</span></div><div class="radar-stat"><strong>—</strong><span>EARLY / DEVELOPING</span></div><div class="radar-stat"><strong>—</strong><span>ACTIONABLE GRADE</span></div><div class="radar-stat"><strong>—</strong><span>PRICE VERIFIED</span></div></div><div id="opportunities" class="opportunity-grid"><div class="empty">Loading candidates…</div></div><div class="tiny footnote">Returns begin at first verified detection, not trade fills. Unverified prices are not treated as confirmed performance. Jarvis never places trades.</div></section>
<details class="archive"><summary><span>Performance & tracking details</span><span class="summary-note">Scorecards, history, and full candidate tables</span></summary><div class="archive-body"><h2 class="archive-title">Performance snapshot</h2><div class="cards" id="stats"></div>
<section class="section"><h2>Stock vs. crypto scoreboard</h2><div class="grid" id="assets"></div></section>
<section class="section"><h2>Performance leaderboard</h2><div class="muted">Top tracked candidates by return from first tracked price.</div><div class="scroll"><table><thead><tr><th>#</th><th>Candidate</th><th>First price</th><th>Latest</th><th>Return</th><th>Best</th><th>Worst</th></tr></thead><tbody id="leaders"></tbody></table></div></section>
<section class="section"><h2>All tracked candidates</h2><div class="toolbar"><input id="q" placeholder="Search ticker…"><select id="assetFilter"><option value="all">All assets</option><option value="stock">Stocks</option><option value="crypto">Crypto</option></select><select id="gradeFilter"><option value="all">All grades</option><option>BUY</option><option>ACTIONABLE SETUP</option><option>DEVELOPING RUNNER</option><option>DEVELOPING SETUP</option><option>EARLY SETUP</option><option>EARLY WATCH</option><option>LATE RUNNER — CHASE RISK</option><option>LOW-CONVICTION WATCH</option><option>PRICE UNVERIFIED — WATCH ONLY</option></select></div><div class="scroll"><table><thead><tr><th>Candidate</th><th>First seen</th><th>First price</th><th>Current grade</th><th>Price source / freshness</th><th>Price as of</th><th>Session move</th><th>Return since first verified</th><th>Best</th><th>Worst</th><th>Age</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="section"><h2>BUY signal performance</h2><div class="muted">Measured from the exact first BUY signal price. Outcomes are recorded at fixed elapsed-time checkpoints; this is a paper record, not executed fills.</div><div class="scroll"><table><thead><tr><th>Signal</th><th>First BUY</th><th>Entry</th><th>Latest return</th><th>1h</th><th>4h</th><th>1d</th><th>3d</th><th>7d</th><th>Score</th></tr></thead><tbody id="buy-history"></tbody></table></div></section><section class="section"><h2>Accuracy scorecard</h2><div class="muted">Current observed outcomes; legacy checkpoint totals may include stock observations made before independent quote validation.</div><div class="grid"><div class="panel"><h3>Return thresholds</h3><div id="thresholds"></div></div><div class="panel"><h3>Lifetime checkpoints</h3><div class="scroll"><table><thead><tr><th>Days</th><th>Observations</th><th>Wins</th><th>Losses</th><th>Average return</th></tr></thead><tbody id="checks"></tbody></table></div></div></div></section>
<section class="section"><h2>Grade movement history</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>From</th><th>To</th><th>When</th></tr></thead><tbody id="moves"></tbody></table></div></section>
<section class="section"><h2>Discovery reasons & catalysts</h2><div class="scroll"><table><thead><tr><th>Candidate</th><th>Signals</th><th>Latest headline / filing</th><th>Risk flags</th></tr></thead><tbody id="catalysts"></tbody></table></div></section>
<section class="section"><h2>Not in latest scan</h2><div class="tiny">Previously tracked, but absent from the latest scan. Last recorded prices may be stale; this is not a sell signal.</div><div class="scroll"><table><thead><tr><th>Candidate</th><th>Last seen</th><th>Last return</th><th>Best gain</th><th>Last grade</th></tr></thead><tbody id="faded"></tbody></table></div></section>
<p class="tiny section">Data may be delayed or incomplete. Candidate discovery is not the same as an actionable alert.</p></div></details></main><script>
const n=v=>Number(v||0),pct=v=>n(v).toFixed(2)+'%',cl=v=>n(v)>0?'pos':n(v)<0?'neg':'',e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"),date=v=>v?new Date(v).toLocaleString():'—';
const rank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1,'PRICE UNVERIFIED — WATCH ONLY':0};
function render(d){const t=d.paper_tracker||{},l=d.lifetime||{},all=Object.values(t),buySignals=d.buy_signals||[],activeBuys=buySignals.filter(x=>x.status==="active"),verified=all.filter(x=>x.price_validation_status!=="unverified"),live=all.filter(x=>x.currently_surfaced!==false),avg=verified.length?verified.reduce((s,x)=>s+n(x.return_pct),0)/verified.length:0;
const fmtTime=d.synced_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(d.synced_at)):'Not yet synced';document.getElementById('updated').textContent='Last synced: '+fmtTime;

const stageRank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1,'PRICE UNVERIFIED — WATCH ONLY':0};
const surfaced=all.filter(x=>x.currently_surfaced!==false);
const earlyCount=surfaced.filter(x=>['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(x.last_stage)).length;
const actionableCount=surfaced.filter(x=>x.last_stage==='ACTIONABLE SETUP').length;
const verifiedCount=surfaced.filter(x=>x.price_validation_status!=='unverified').length;
document.getElementById('radar-stats').innerHTML=[[''+activeBuys.length,'ACTIVE BUY SIGNALS'],[''+surfaced.length,'ON RADAR'],[''+earlyCount,'EARLY / DEVELOPING'],[''+actionableCount,'ACTIONABLE SETUPS'],[''+verifiedCount,'PRICE VERIFIED']].map(x=>'<div class="radar-stat"><strong>'+e(x[0])+'</strong><span>'+x[1]+'</span></div>').join('');
document.getElementById('buy-summary').textContent=activeBuys.length+' active · '+buySignals.length+' total logged';
const recentBuys=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,4);
document.getElementById('buy-signals').innerHTML=recentBuys.length?recentBuys.map(x=>{const ret=x.latest_return_pct==null?'Awaiting update':pct(x.latest_return_pct);const reason=(x.reasons||[]).slice(0,2).join(' · ');return '<article class="buy-card"><div class="buy-card-top"><div><div class="buy-tag">'+(x.status==='active'?'BUY · ACTIVE':'BUY · HISTORICAL')+'</div><div class="buy-symbol">'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</div></div><div class="buy-return '+cl(x.latest_return_pct)+'">'+e(ret)+'</div></div><div class="buy-detail">Signal price: '+e(x.entry_price)+' · Score: '+e(x.score)+'/100<br>Entry zone: '+e((x.entry_low??'—')+' – '+(x.entry_high??'—'))+'<br>Stop-loss: '+e(x.stop_loss??x.invalidation??'—')+' ('+e(x.risk_pct==null?'—':x.risk_pct+'% risk')+')<br>Potential sell: '+e(x.target_1??'—')+' ('+e(x.target_1_pct==null?'—':'+'+x.target_1_pct+'%')+') · Stretch target: '+e(x.target_2??'—')+' ('+e(x.target_2_pct==null?'—':'+'+x.target_2_pct+'%')+')<br>Reward/risk: '+e(x.reward_risk_ratio==null?'—':x.reward_risk_ratio+':1')+'</div><div class="buy-meta">'+date(x.at)+' · '+e(reason)+'</div></article>'}).join(''):'<div class="op-empty">No BUY signals recorded yet. Jarvis will keep watching rather than manufacture a trade.</div>';
const candidates=surfaced.sort((a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)||n(b.daily_change_pct)-n(a.daily_change_pct)).slice(0,9);
document.getElementById('opportunities').innerHTML=candidates.length?candidates.map((x,i)=>{
 const stage=x.last_stage||'WATCHLIST',s=String(stage),kind=s.includes('ACTIONABLE')?'action':s.includes('LATE RUNNER')?'late':'';
 const reason=(x.stage_reasons||x.discovery_reasons||[]).slice(0,2).join(' · ')||'Candidate retained for continued monitoring.';
 const risks=(x.risk_flags||[]).slice(0,2).join(' · ');
 const price=x.latest_price==null?'—':(Number.isFinite(Number(x.latest_price))?Number(x.latest_price).toPrecision(7):'—');
 const day=x.daily_change_pct==null?'—':pct(x.daily_change_pct);
 const ret=x.price_validation_status==='unverified'?'Unverified':pct(x.return_pct);
 return '<article class="op-card '+(i===0?'featured':'')+'"><div class="op-card-top"><div><div class="op-kind">'+(i===0?'TOP RADAR CANDIDATE · ':'')+e((x.asset_type||'').toUpperCase())+'</div><div class="op-symbol">'+e(x.symbol||'—')+'</div></div><span class="op-stage '+kind+'">'+e(stage)+'</span></div><div class="op-metrics"><div class="op-metric"><strong>'+e(price)+'</strong><span>LATEST TRACKED PRICE</span></div><div class="op-metric"><strong class="'+cl(x.daily_change_pct)+'">'+e(day)+'</strong><span>DAILY MOVE</span></div><div class="op-metric"><strong class="'+cl(x.return_pct)+'">'+e(ret)+'</strong><span>SINCE FIRST VERIFIED</span></div></div><div class="op-reason">'+e(reason)+'</div>'+(risks?'<div class="op-risk">RISK: '+e(risks)+'</div>':'')+'</article>';
}).join(''):'<div class="op-empty">Nothing has qualified for the radar yet. Jarvis will keep monitoring and show candidates after a successful scan.</div>';

const thresholds=[['Positive now',verified.filter(x=>n(x.return_pct)>0).length],['At least +5%',verified.filter(x=>n(x.return_pct)>=5).length],['At least +10%',verified.filter(x=>n(x.return_pct)>=10).length],['At least +20%',verified.filter(x=>n(x.return_pct)>=20).length],['Down 5% or more',verified.filter(x=>n(x.return_pct)<=-5).length],['Ever reached +10%',verified.filter(x=>n(x.max_gain_pct)>=10).length]];
document.getElementById('stats').innerHTML=[['Tracked candidates',all.length],['In latest scan',live.length],['Average return',pct(avg)],['Positive candidates',thresholds[0][1]+' / '+verified.length],['Lifetime checkpoint wins',Object.values(l.checkpoints||{}).reduce((s,x)=>s+n(x.wins),0)],['Top current return',pct(Math.max(0,...verified.map(x=>n(x.return_pct))))]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');
document.getElementById('assets').innerHTML=['stock','crypto'].map(k=>{const a=verified.filter(x=>x.asset_type===k),m=a.length?a.reduce((s,x)=>s+n(x.return_pct),0)/a.length:0;return '<div class="panel"><div class="muted">'+(k==='stock'?'Stocks':'Crypto')+'</div><div class="value">'+a.length+' tracked</div><div class="'+cl(m)+'">Average return '+pct(m)+'</div><div class="tiny">'+a.filter(x=>n(x.return_pct)>0).length+' positive · '+a.filter(x=>x.last_stage==='ACTIONABLE SETUP').length+' actionable grade</div></div>'}).join('');
document.getElementById('buy-history').innerHTML=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'<div class="tiny">'+e(x.status||'')+'</div></td><td>'+date(x.at)+'</td><td>'+e(x.entry_price)+'</td><td class="'+cl(x.latest_return_pct)+'">'+(x.latest_return_pct==null?'—':pct(x.latest_return_pct))+'</td>'+['1h','4h','1d','3d','7d'].map(h=>'<td class="'+cl(x.outcomes&&x.outcomes[h]&&x.outcomes[h].return_pct)+'">'+(x.outcomes&&x.outcomes[h]?pct(x.outcomes[h].return_pct):'—')+'</td>').join('')+'<td>'+e(x.score)+'/100</td></tr>').join('')||'<tr><td colspan="10" class="empty">No BUY recommendations have been recorded yet.</td></tr>';
const byReturn=[...verified].sort((a,b)=>n(b.return_pct)-n(a.return_pct));document.getElementById('leaders').innerHTML=byReturn.slice(0,10).map((x,i)=>'<tr><td>'+(i+1)+'</td><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td>'+n(x.latest_price).toPrecision(7)+'</td><td class="'+cl(x.return_pct)+'">'+pct(x.return_pct)+'</td><td class="pos">'+pct(x.max_gain_pct)+'</td><td class="neg">'+pct(x.max_drawdown_pct)+'</td></tr>').join('')||'<tr><td colspan="7" class="empty">No tracked candidates yet.</td></tr>';
function rows(){const q=document.getElementById('q').value.toUpperCase(),a=document.getElementById('assetFilter').value,g=document.getElementById('gradeFilter').value;const v=all.filter(x=>(a==='all'||x.asset_type===a)&&(g==='all'||x.last_stage===g)&&(!q||String(x.symbol).toUpperCase().includes(q)));v.sort((x,y)=>(rank[y.last_stage]||0)-(rank[x.last_stage]||0)||(y.last_seen||'').localeCompare(x.last_seen||''));document.getElementById('rows').innerHTML=v.map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.first_seen)+'</td><td>'+n(x.initial_price).toPrecision(7)+'</td><td><span class="pill">'+e(x.last_stage||'—')+'</span></td><td>'+e(x.price_source||'Legacy record — source not recorded')+'<div class="tiny">'+e(x.price_freshness||'Freshness unknown')+(x.price_validation_status==='unverified'?' · PRICE UNVERIFIED — excluded from returns':'')+'</div></td><td>'+date(x.price_as_of||x.last_seen)+'</td><td class="'+cl(x.session_change_pct)+'">'+(x.price_validation_status==='unverified'||x.session_change_pct==null?'—':pct(x.session_change_pct))+'</td><td class="'+cl(x.return_pct)+'">'+(x.price_validation_status==='unverified'?'UNVERIFIED':pct(x.return_pct))+'</td><td class="pos">'+(x.price_validation_status==='unverified'?'—':pct(x.max_gain_pct))+'</td><td class="neg">'+(x.price_validation_status==='unverified'?'—':pct(x.max_drawdown_pct))+'</td><td>'+n(x.age_days).toFixed(1)+'d</td></tr>').join('')||'<tr><td colspan="11" class="empty">No matches.</td></tr>'}
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

    # GitHub Actions commits these tracker files after each scan. Read the
    # committed files as the single source of truth; never prefer a Render-local
    # snapshot, which can be stale or disappear during a redeploy.
    paper = read_state(PAPER_STATE)
    lifetime = read_state(PAPER_LIFETIME_STATE)
    buy_state = read_state(BUY_SIGNAL_STATE)
    timestamps = [
        path.stat().st_mtime
        for path in (PAPER_STATE, PAPER_LIFETIME_STATE, BUY_SIGNAL_STATE)
        if path.exists()
    ]
    synced_at = datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat() if timestamps else None
    return {"synced_at": synced_at, "paper_tracker": paper, "lifetime": lifetime, "buy_signals": buy_state.get("signals", [])}


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
        },
    }
