"""Read-only, on-demand ticker case studies from existing Jarvis research history.

No fresh quotes, external lookups, scanner runs, BUY rules, or paper trades.
"""
from __future__ import annotations

import re

from early_opportunity_radar import _eligible_snapshot

MAX_TICKERS = 10
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")


def _first_sorted(rows, key):
    valid = [row for row in (rows or []) if isinstance(row, dict) and row.get(key)]
    return min(valid, key=lambda row: str(row[key])) if valid else None


def _gate_details(event):
    if not event:
        return None
    return {
        "at": event.get("at"),
        "price": event.get("price"),
        "score": event.get("score"),
        "stage": event.get("stage"),
        "readiness_pct": event.get("readiness_pct"),
        "market_regime": event.get("market_regime"),
        "failed_gate_keys": list(event.get("failed_gate_keys") or [])[:16],
        "price_verified": event.get("price_verified") is True,
    }


def _case(record):
    history = [s for s in (record.get("signal_history") or []) if isinstance(s, dict)]
    gate_events = [s for s in (record.get("buy_gate_history") or []) if isinstance(s, dict)]
    first_signal = _first_sorted(history, "observed_at")
    fresh_signals = [s for s in history if _eligible_snapshot(s)]
    first_fresh = _first_sorted(fresh_signals, "observed_at")
    first_gate = _first_sorted(gate_events, "at")
    last_gate = max((s for s in gate_events if s.get("at")), key=lambda s: str(s["at"]), default=None)
    promotions = [s for s in (record.get("grade_history") or [])
                  if isinstance(s, dict) and str(s.get("to") or "").upper() in {
                      "EARLY SETUP", "DEVELOPING SETUP", "DEVELOPING RUNNER",
                      "ACTIONABLE SETUP", "BUY"
                  }]
    first_promotion = _first_sorted(promotions, "at")
    baseline_status = str(record.get("discovery_baseline_status") or "unknown")
    return {
        "found": True,
        "symbol": str(record.get("symbol") or "").upper(),
        "asset_type": str(record.get("asset_type") or "").lower(),
        "first_seen": record.get("first_seen"),
        "last_seen": record.get("last_seen"),
        "latest_stage": record.get("last_stage"),
        "latest_price_status": record.get("price_validation_status") or "unknown",
        "first_research_signal": ({
            "at": first_signal.get("observed_at"),
            "label": first_signal.get("shadow_classification"),
            "stage": first_signal.get("stage"),
            "price": first_signal.get("price"),
            "quote_as_of": first_signal.get("price_as_of"),
            "outcome_eligible": _eligible_snapshot(first_signal),
        } if first_signal else None),
        "first_fresh_signal": ({
            "at": first_fresh.get("observed_at"),
            "price": first_fresh.get("price"),
            "quote_as_of": first_fresh.get("price_as_of"),
            "stage": first_fresh.get("stage"),
        } if first_fresh else None),
        "first_stage_promotion": ({
            "at": first_promotion.get("at"),
            "stage": first_promotion.get("to"),
        } if first_promotion else None),
        "first_buy_gate_check": _gate_details(first_gate),
        "latest_buy_gate_check": _gate_details(last_gate),
        "retained_research_snapshots": len(history),
        "fresh_outcome_snapshots": len(fresh_signals),
        "rejected_legacy_outcome_flags": sum(
            s.get("outcome_eligible") is True and not _eligible_snapshot(s)
            for s in history
        ),
        "discovery_baseline_status": baseline_status,
        "discovery_first_accepted_price": (
            record.get("discovery_initial_price")
            if baseline_status in {"live_verified", "first_verified_fallback"} else None
        ),
        "discovery_first_quote_at": (
            record.get("discovery_price_as_of")
            if baseline_status in {"live_verified", "first_verified_fallback"} else None
        ),
        "discovery_outcome_status": record.get("discovery_return_status") or "unknown",
        "verified_discovery_max_gain_pct": (
            record.get("discovery_max_gain_pct")
            if record.get("discovery_return_status") == "verified" else None
        ),
    }


def build_runner_case_studies(records, symbols):
    """Investigate up to ten specific symbols without changing Jarvis's research records."""
    tokens = [str(x).strip().upper() for x in
              (symbols.split(",") if isinstance(symbols, str) else symbols or [])]
    unique = []
    for token in tokens:
        if token and token not in unique:
            if not SYMBOL_PATTERN.fullmatch(token):
                raise ValueError("Symbols must contain letters, numbers, periods or hyphens.")
            unique.append(token)
    if not unique or len(unique) > MAX_TICKERS:
        raise ValueError("Enter between 1 and 10 stock or crypto symbols.")
    by_symbol = {}
    for row in records or []:
        if isinstance(row, dict):
            symbol = str(row.get("symbol") or "").upper()
            asset = str(row.get("asset_type") or "").lower()
            if symbol in unique and asset in {"stock", "crypto"}:
                by_symbol.setdefault(symbol, []).append(row)
    cases = []
    for token in unique:
        found = by_symbol.get(token) or []
        if not found:
            cases.append({"symbol": token, "found": False,
                          "interpretation": "No record in retained candidate history; does not prove it was never scanned."})
        else:
            cases.extend(_case(record) for record in sorted(found, key=lambda v: str(v.get("asset_type"))))
    return {
        "read_only": True,
        "symbols": unique,
        "cases": cases,
        "limits": [
            "First seen is a recorded discovery timestamp, not proof a safe earlier entry was available.",
            "The per-scan research signal history retains only the most recent 32 observations per candidate.",
            "Only fresh, independently timestamped observations count toward outcome analysis.",
            "A blocked BUY can have multiple overlapping causes; these records do not rank their causal importance.",
            "Missing history does not prove Jarvis never scanned or saw a symbol.",
            "No prices, trading decisions, scores, data sources, fees or risk rules are altered.",
        ],
    }
