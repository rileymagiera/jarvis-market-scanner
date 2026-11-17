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
