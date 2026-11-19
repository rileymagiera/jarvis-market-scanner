"""Research-only early-runner feature engineering. No fields from this module alter BUY scoring."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import math
import pandas as pd


SECTOR_ETFS = {
    "Technology": "XLK", "Healthcare": "XLV", "Financial Services": "XLF",
    "Financial": "XLF", "Energy": "XLE", "Industrials": "XLI",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
    "Basic Materials": "XLB", "Real Estate": "XLRE",
    "Communication Services": "XLC", "Utilities": "XLU",
}


def _num(value: Any, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _local_index(frame):
    idx = frame.index
    return idx.tz_convert("America/New_York") if getattr(idx, "tz", None) is not None else idx


def _catalyst_assessment(news, fundamentals):
    headlines = [str(x.get("title") or "") for x in (news or []) if isinstance(x, dict)]
    text = " ".join(headlines).lower()
    positive_terms = {
        "fda approval": "FDA approval", "fda clearance": "FDA clearance",
        "phase 3": "Phase 3 results", "phase iii": "Phase III results",
        "clinical trial": "Clinical trial update", "contract": "Contract award",
        "partnership": "Partnership", "acquisition": "Acquisition",
        "merger": "Merger", "earnings beat": "Earnings beat",
        "raises guidance": "Raised guidance", "record revenue": "Record revenue",
        "strategic investment": "Strategic investment",
    }
    risk_terms = {
        "offering": "Offering language in headline", "registered direct": "Registered direct offering",
        "at-the-market": "ATM financing", "atm program": "ATM financing",
        "shelf registration": "Shelf registration", "warrant": "Warrant-related headline",
        "convertible": "Convertible financing", "dilutive": "Dilution language",
        "reverse split": "Reverse split", "going concern": "Going-concern warning",
    }
    catalysts = sorted({label for term, label in positive_terms.items() if term in text})
    headline_risks = sorted({label for term, label in risk_terms.items() if term in text})
    filing_risks = []
    for item in (news or []):
        if not isinstance(item, dict):
            continue
        form = str(item.get("form") or "").upper()
        title = str(item.get("title") or "").lower()
        if form in {"S-1", "S-3", "424B1", "424B3", "424B5"}:
            filing_risks.append("SEC " + form + " registration / offering filing")
        if form == "8-K" and any(k in title for k in ("offering", "purchase agreement", "sales agreement", "warrant")):
            filing_risks.append("8-K financing-related filing; document review required")
    risks = sorted(set(headline_risks + filing_risks))
    fields = (fundamentals or {}).get("fields") or {}
    float_shares = _num(fields.get("float_shares"))
    shares = _num(fields.get("shares_outstanding"))
    short_float = _num(fields.get("short_percent_float"))
    if float_shares is None and shares:
        risks.append("Public float unavailable; shares outstanding is not a float substitute")
    status = "Material catalyst keywords detected" if catalysts else "No classified catalyst in available headlines"
    if risks:
        status += " · financing/dilution review required"
    return {
        "catalyst_labels": catalysts,
        "catalyst_dilution_status": status,
        "dilution_flags": sorted(set(risks)),
        "float_shares": float_shares,
        "shares_outstanding": shares,
        "short_percent_float": short_float,
        "catalyst_headline_count": len(headlines),
    }


def compute_early_runner_metrics(daily, intraday, price, fundamentals=None, news=None):
    """Compute transparent research features from already-fetched data only."""
    result = {
        "premarket_gap_pct": None, "premarket_volume": None,
        "premarket_rvol_same_time": None, "premarket_high": None,
        "premarket_low": None, "cumulative_rvol_same_time": None,
        "volume_acceleration_3bar": None, "float_rotation_pct": None,
        "higher_highs_higher_lows": None, "pullback_from_session_high_pct": None,
        "premarket_high_reclaimed": None, "sector_etf": None,
        "sector_relative_strength_5d": None, "early_runner_research_score": 0,
        "early_runner_score_components": {},
    }
    previous_close = None
    if daily is not None and not daily.empty and len(daily) >= 2:
        previous_close = _num(daily["Close"].iloc[-2])

    float_data = _catalyst_assessment(news, fundamentals)
    result.update(float_data)
    float_shares = _num(result.get("float_shares"))
    bars = intraday.copy() if intraday is not None else pd.DataFrame()
    if not bars.empty and {"High", "Low", "Close", "Volume"}.issubset(bars.columns):
        bars = bars.dropna(subset=["Close"]).copy()
        idx = _local_index(bars)
        bars["_date"] = [x.date() for x in idx]
        bars["_time"] = [x.strftime("%H:%M") for x in idx]
        today = max(bars["_date"])
        session = bars[bars["_date"] == today].copy()
        if not session.empty:
            local_times = [x for x in idx[bars.index.get_indexer(session.index)]]
            pre_mask = [x.time() < datetime.strptime("09:30", "%H:%M").time() for x in local_times]
            pre = session.loc[pre_mask]
            regular = session.loc[[not x for x in pre_mask]]
            if not pre.empty:
                pre_volume = _num(pre["Volume"].fillna(0).sum(), 0.0)
                result["premarket_volume"] = round(pre_volume, 0)
                result["premarket_high"] = round(_num(pre["High"].max(), 0), 6)
                result["premarket_low"] = round(_num(pre["Low"].min(), 0), 6)
                first_open = _num(pre["Open"].iloc[0]) if "Open" in pre.columns else None
                if first_open and previous_close and previous_close > 0:
                    result["premarket_gap_pct"] = round((first_open / previous_close - 1) * 100, 3)
                past = bars[bars["_date"] < today]
                if not past.empty:
                    past_times = _local_index(past)
                    cutoff = _local_index(session)[-1].time()
                    prior_pre = past[[t.time() < datetime.strptime("09:30", "%H:%M").time() and t.time() <= cutoff
                                      for t in past_times]]
                else:
                    prior_pre = past
                daily_pre = prior_pre.groupby("_date")["Volume"].sum() if not prior_pre.empty else pd.Series(dtype=float)
                baseline = _num(daily_pre.median()) if len(daily_pre) >= 2 else None
                if baseline and baseline > 0:
                    result["premarket_rvol_same_time"] = round(pre_volume / baseline, 2)
            elif not regular.empty and previous_close and previous_close > 0 and "Open" in regular.columns:
                first_open = _num(regular["Open"].iloc[0])
                if first_open:
                    result["premarket_gap_pct"] = round((first_open / previous_close - 1) * 100, 3)
            if not regular.empty:
                current_time = session["_time"].iloc[-1]
                prior = bars[(bars["_date"] < today) & (bars["_time"] <= current_time)]
                prior_cum = prior.groupby("_date")["Volume"].sum() if not prior.empty else pd.Series(dtype=float)
                current_cum = _num(session["Volume"].fillna(0).sum(), 0.0)
                base_cum = _num(prior_cum.median()) if len(prior_cum) >= 2 else None
                if base_cum and base_cum > 0:
                    result["cumulative_rvol_same_time"] = round(current_cum / base_cum, 2)
                vols = regular["Volume"].astype(float).fillna(0)
                if len(vols) >= 6:
                    recent = _num(vols.tail(3).mean(), 0.0)
                    previous = _num(vols.iloc[-6:-3].mean(), 0.0)
                    if previous > 0:
                        result["volume_acceleration_3bar"] = round(recent / previous, 2)
                if float_shares and float_shares > 0:
                    result["float_rotation_pct"] = round(current_cum / float_shares * 100, 3)
                if len(regular) >= 4:
                    highs = regular["High"].astype(float).tail(4).to_list()
                    lows = regular["Low"].astype(float).tail(4).to_list()
                    result["higher_highs_higher_lows"] = bool(
                        all(highs[i] >= highs[i-1] for i in range(1, 4)) and
                        all(lows[i] >= lows[i-1] for i in range(1, 4))
                    )
                session_high = _num(session["High"].max())
                last_close = _num(session["Close"].iloc[-1])
                if session_high and session_high > 0 and last_close:
                    result["pullback_from_session_high_pct"] = round((last_close / session_high - 1) * 100, 3)
                if result.get("premarket_high") and last_close:
                    result["premarket_high_reclaimed"] = bool(last_close > result["premarket_high"])

    components = {
        "volume_acceleration": 15 if (_num(result.get("volume_acceleration_3bar"), 0) or 0) >= 1.8 else 0,
        "cumulative_rvol": 20 if (_num(result.get("cumulative_rvol_same_time"), 0) or 0) >= 2.0 else 10 if (_num(result.get("cumulative_rvol_same_time"), 0) or 0) >= 1.5 else 0,
        "float_rotation": 20 if (_num(result.get("float_rotation_pct"), 0) or 0) >= 50 else 10 if (_num(result.get("float_rotation_pct"), 0) or 0) >= 20 else 0,
        "price_structure": 15 if result.get("higher_highs_higher_lows") else 0,
        "premarket_reclaim": 10 if result.get("premarket_high_reclaimed") else 0,
        "catalyst": 10 if result.get("catalyst_labels") else 0,
        "dilution_risk_penalty": -15 if result.get("dilution_flags") else 0,
    }
    result["early_runner_score_components"] = components
    result["early_runner_research_score"] = max(0, min(100, sum(components.values())))
    matches = []
    if components["volume_acceleration"]:
        matches.append("Intraday volume acceleration")
    if components["cumulative_rvol"]:
        matches.append("Cumulative same-time RVOL")
    if components["float_rotation"]:
        matches.append("Float rotation")
    if components["price_structure"]:
        matches.append("Higher highs / higher lows")
    if components["premarket_reclaim"]:
        matches.append("Premarket high reclaim")
    if components["catalyst"]:
        matches.append("Classified catalyst")
    if result.get("dilution_flags"):
        matches.append("Dilution / financing risk flag")
    result["early_runner_signal_matches"] = matches
    return result




def build_data_quality_record(asset_type, price_source, price_freshness, observed_at,
                              received_at, session=None, validation_passed=False,
                              provider_feed=None, price_age_seconds=None,
                              validation_note=None):
    """Classify the selected price without conflating live, delayed, EOD, or unknown data."""
    from datetime import datetime, timezone

    now = datetime.fromisoformat(received_at.replace("Z", "+00:00")) if received_at else datetime.now(timezone.utc)
    observed = None
    try:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")) if observed_at else None
        if observed and observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if observed:
            observed = observed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        observed = None

    age_seconds = _num(price_age_seconds)
    if age_seconds is None and observed is not None:
        age_seconds = (now.astimezone(timezone.utc) - observed).total_seconds()
    if age_seconds is not None:
        age_seconds = round(age_seconds, 1)

    source = str(price_source or "").lower()
    freshness = str(price_freshness or "").lower()
    note = str(validation_note or "").lower()
    phase = str((session or {}).get("phase") or "").upper()
    data_type = "unknown"

    if asset_type == "crypto":
        if validation_passed and observed is not None and age_seconds is not None and -30 <= age_seconds <= 300:
            data_type = "live"
    elif "prior_session_close" in freshness:
        data_type = "eod"
    elif "fresh_multi_provider" in freshness:
        if "massive" in source or str(provider_feed or "").lower() == "delayed_sip":
            data_type = "delayed"
        elif ("alpaca" in source and str(provider_feed or "").lower() in {"iex", "sip"}
              and age_seconds is not None and -30 <= age_seconds <= 120):
            data_type = "live"
    elif "fresh_webull" in freshness:
        # Webull's subscription delay entitlement is not currently confirmed in Jarvis.
        data_type = "unknown"
    elif "daily_bar" in freshness:
        if observed is not None and ("CLOSED" in phase or "WEEKEND" in phase):
            data_type = "eod"
        elif observed is not None:
            data_type = "historical"

    max_age = 300 if asset_type == "crypto" else 900
    if data_type == "unknown" and age_seconds is not None and age_seconds > max_age:
        verification_status = "stale"
    elif "disagree" in note or "mismatch" in note or "conflict" in note:
        verification_status = "conflicting"
    elif validation_passed and data_type != "unknown":
        verification_status = "verified"
    else:
        verification_status = "unknown"

    regular_session = "REGULAR SESSION" in phase
    buy_eligible = bool(
        validation_passed and data_type == "live"
        and (asset_type == "crypto" or regular_session)
    )
    return {
        "data_source": price_source,
        "data_type": data_type,
        "observed_at": observed.isoformat() if observed else observed_at,
        "received_at": now.astimezone(timezone.utc).isoformat(),
        "age_seconds": age_seconds,
        "session": phase or "UNKNOWN",
        "verification_status": verification_status,
        "buy_eligible": buy_eligible,
        "note": (
            "Live, timestamped and eligible for the existing session-specific BUY checks."
            if buy_eligible else
            "Research context only; this price type does not qualify as a live BUY reference."
            if data_type in {"delayed", "eod", "historical"} else
            "Freshness or provider entitlement is not established; BUY use is blocked."
        ),
    }


def apply_entry_quality_gate(stage, data_quality, price_validation_passed, buy_readiness):
    """Keep research discovery visible while requiring verified live data for BUY."""
    result = {
        "stage": stage,
        "potential_stage": None,
        "entry_status": "RESEARCH ONLY",
        "signal_blocked_reason": None,
    }
    quality = data_quality or {}
    readiness = buy_readiness or {}
    if stage == "BUY":
        if (readiness.get("all_gates_passed") is True
                and price_validation_passed is True
                and quality.get("buy_eligible") is True):
            result["entry_status"] = "LIVE DATA VERIFIED — BUY GATES PASSED"
        else:
            result["potential_stage"] = "BUY"
            result["stage"] = "ACTIONABLE SETUP"
            if not price_validation_passed:
                result["entry_status"] = "WAITING FOR INDEPENDENT PRICE VALIDATION"
                result["signal_blocked_reason"] = "Independent current-price validation is unavailable or stale"
            elif quality.get("buy_eligible") is not True:
                kind = str(quality.get("data_type") or "unknown").upper()
                result["entry_status"] = "WAITING FOR LIVE ENTRY CONFIRMATION"
                result["signal_blocked_reason"] = f"Selected price is {kind}; live timestamped data is required for BUY"
            else:
                result["entry_status"] = "BUY GATES INCOMPLETE"
                result["signal_blocked_reason"] = "One or more strict BUY readiness checks did not pass"
    elif not price_validation_passed:
        result["entry_status"] = "RESEARCH ONLY — PRICE UNVERIFIED"
        result["signal_blocked_reason"] = "Candidate remains visible for discovery; current price is not independently verified"
    elif quality.get("buy_eligible") is not True:
        result["entry_status"] = f"RESEARCH ONLY — {str(quality.get('data_type') or 'unknown').upper()} DATA"
        result["signal_blocked_reason"] = "Candidate remains visible for discovery; this data type cannot confirm a live entry"
    return result

def set_crypto_relative_strength_coverage(coverage, relative):
    """Keep legacy and canonical flags aligned for BTC-relative strength coverage."""
    available = relative is not None
    coverage["relative_strength_vs_btc"] = available
    coverage["market_relative_strength"] = available
    return coverage


def summarize_crypto_cross_section(crypto_sectors, pair_map, tickers):
    """Summarize broad exchange tickers; independent of the scanner's candidate filter."""
    returns_by_sector = {}
    expected_by_sector = {}
    for symbol, sector in crypto_sectors.items():
        base = str(symbol or "").upper()
        for suffix in ("USDT", "USD", "USDC"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        base = {"XBT": "BTC", "XDG": "DOGE"}.get(base, base)
        pair_id = pair_map.get(base)
        if not pair_id:
            continue
        expected_by_sector[sector] = expected_by_sector.get(sector, 0) + 1
        ticker = (tickers or {}).get(pair_id)
        if not isinstance(ticker, dict):
            continue
        close_rows = ticker.get("c") or []
        last = _num(close_rows[0] if close_rows else None)
        opening = _num(ticker.get("o"))
        if last is None or opening is None or last <= 0 or opening <= 0:
            continue
        returns_by_sector.setdefault(sector, []).append((last / opening - 1) * 100)

    btc_pair = pair_map.get("BTC")
    btc_ticker = (tickers or {}).get(btc_pair) if btc_pair else None
    btc_close = (btc_ticker or {}).get("c") or []
    btc_last = _num(btc_close[0] if btc_close else None)
    btc_open = _num((btc_ticker or {}).get("o"))
    btc_change = (btc_last / btc_open - 1) * 100 if btc_last and btc_open and btc_last > 0 and btc_open > 0 else None

    stats = {}
    for sector, expected in expected_by_sector.items():
        values = returns_by_sector.get(sector, [])
        average = sum(values) / len(values) if values else None
        breadth = sum(value > 0 for value in values) / len(values) * 100 if values else None
        coverage = len(values) / expected if expected else 0
        stats[sector] = {
            "avg_24h_change_pct": round(average, 2) if average is not None else None,
            "positive_breadth_pct": round(breadth, 1) if breadth is not None else None,
            "sample_size": len(values),
            "universe_size": expected,
            "coverage_pct": round(coverage * 100, 1),
            "reliable": len(values) >= 3 and coverage >= 0.5,
            "source": "Kraken full-market 24h ticker",
        }
    return {
        "btc_change_pct": round(btc_change, 4) if btc_change is not None else None,
        "btc_source": "Kraken full-market 24h ticker" if btc_change is not None else None,
        "sector_stats": stats,
    }



def compute_early_warning_metrics(daily, intraday, price):
    """Research-only early-warning features; never bypass BUY quality or risk gates."""
    result = {
        "breakout_proximity_pct": None,
        "breakout_reference_price": None,
        "breakout_proximity_status": "Unavailable",
        "liquidity_sweep_detected": None,
        "liquidity_sweep_status": "Intraday history unavailable",
        "accumulation_signal_score": None,
        "accumulation_signal_status": "Insufficient evidence",
        "multi_timeframe_alignment": None,
        "multi_timeframe_status": "Insufficient timeframe data",
        "early_warning_matches": [],
    }
    matches = []
    try:
        px = float(price)
    except (TypeError, ValueError, OverflowError):
        px = 0.0
    if daily is not None and not daily.empty and {"High", "Low", "Close", "Volume"}.issubset(daily.columns):
        d = daily.dropna(subset=["High", "Low", "Close"]).copy()
        if len(d) >= 21 and px > 0:
            prior_resistance = float(d["High"].astype(float).iloc[-21:-1].max())
            distance = (prior_resistance / px - 1) * 100 if px > 0 else None
            if distance is not None and np.isfinite(distance):
                result["breakout_reference_price"] = round(prior_resistance, 6)
                result["breakout_proximity_pct"] = round(distance, 2)
                if -0.25 <= distance <= 3.0:
                    result["breakout_proximity_status"] = f"Near 20-session high · {distance:.2f}%"
                    matches.append("Near 20-session breakout level")
                elif distance < -0.25:
                    result["breakout_proximity_status"] = f"Above prior 20-session high · {-distance:.2f}%"
                    matches.append("Trading above prior 20-session high")
                else:
                    result["breakout_proximity_status"] = f"{distance:.2f}% below prior 20-session high"
        # Compression relative to the asset's own 60-session volatility history.
        close = d["Close"].astype(float)
        width = (close.rolling(20, min_periods=20).std(ddof=0) * 4 /
                 close.rolling(20, min_periods=20).mean().replace(0, np.nan)).dropna()
        if len(width) >= 20:
            percentile = float((width.tail(60) <= width.iloc[-1]).mean() * 100)
            result["volatility_compression_percentile"] = round(percentile, 1)
            if percentile <= 20:
                matches.append("Volatility compressed versus own history")
        # Multi-timeframe daily direction; intraday confirmation added below.
        daily_bullish = bool(
            len(close) >= 20
            and close.iloc[-1] > close.ewm(span=9, adjust=False).mean().iloc[-1]
            and close.ewm(span=9, adjust=False).mean().iloc[-1] >
                close.ewm(span=20, adjust=False).mean().iloc[-1]
        )
    else:
        daily_bullish = None

    bars = intraday.copy() if intraday is not None else pd.DataFrame()
    intraday_bullish = None
    if not bars.empty and {"High", "Low", "Close", "Volume"}.issubset(bars.columns):
        bars = bars.dropna(subset=["High", "Low", "Close"]).copy()
        if len(bars) >= 8:
            high = bars["High"].astype(float)
            low = bars["Low"].astype(float)
            close = bars["Close"].astype(float)
            volume = bars["Volume"].astype(float).fillna(0)
            prior_floor = float(low.iloc[-8:-1].min())
            current_low = float(low.iloc[-1])
            current_close = float(close.iloc[-1])
            swept = current_low < prior_floor and current_close > prior_floor
            result["liquidity_sweep_detected"] = bool(swept)
            result["liquidity_sweep_status"] = (
                f"Downside liquidity sweep reclaimed · level {prior_floor:.4f}"
                if swept else "No confirmed downside sweep/reclaim in latest bar"
            )
            if swept:
                matches.append("Downside liquidity sweep reclaimed")
            if len(close) >= 6:
                recent = close.tail(5)
                intraday_bullish = bool(recent.iloc[-1] > recent.iloc[0] and
                                        recent.iloc[-1] >= recent.rolling(3).mean().iloc[-1])
            # Simple accumulation evidence: positive OBV slope and rising recent
            # volume versus the immediately preceding bars; missing inputs stay unknown.
            direction = np.sign(close.diff()).fillna(0)
            obv = (direction * volume).cumsum()
            obv_up = bool(len(obv) >= 6 and obv.iloc[-1] > obv.iloc[-6])
            recent_volume = float(volume.tail(3).mean())
            prior_volume = float(volume.iloc[-6:-3].mean()) if len(volume) >= 6 else 0
            volume_building = prior_volume > 0 and recent_volume >= prior_volume * 1.2
            evidence = [obv_up, volume_building]
            result["accumulation_signal_score"] = int(sum(evidence) / len(evidence) * 100)
            result["accumulation_signal_status"] = (
                "OBV rising + volume building" if all(evidence)
                else "Partial accumulation evidence" if any(evidence)
                else "No accumulation confirmation"
            )
            if any(evidence):
                matches.append("Accumulation evidence developing")
    result["multi_timeframe_alignment"] = (
        bool(daily_bullish and intraday_bullish)
        if daily_bullish is not None and intraday_bullish is not None else None
    )
    if result["multi_timeframe_alignment"] is True:
        result["multi_timeframe_status"] = "Daily and intraday trends aligned bullish"
        matches.append("Daily + intraday trend alignment")
    elif result["multi_timeframe_alignment"] is False:
        result["multi_timeframe_status"] = "Daily/intraday trend not fully aligned"
    result["early_warning_matches"] = matches
    return result
