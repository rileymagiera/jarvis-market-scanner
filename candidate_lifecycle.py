"""Research-only lifecycle snapshots for BUY gate failures and catalysts."""
from __future__ import annotations

from datetime import datetime, timezone
import json


def _safe_float(value):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_utc(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def update_candidate_lifecycle(record, item, now, observation_eligible=False):
    """Append bounded audit snapshots without changing any score or eligibility."""
    readiness = item.get("buy_readiness") or {}
    checks = readiness.get("checks") if isinstance(readiness.get("checks"), dict) else {}
    failed = sorted(str(key) for key, passed in checks.items() if not passed)
    stage = str(item.get("stage") or item.get("signal") or "WATCH")
    score = _safe_float(item.get("score"))
    research_score = _safe_float(item.get("early_runner_research_score"))
    percent = _safe_float(readiness.get("percent"))

    # Keep a durable record for candidates with meaningful discovery evidence.
    promising = (
        stage not in {"LOW-CONVICTION WATCH", "WATCH", "PRICE UNVERIFIED — WATCH ONLY"}
        or (percent is not None and percent >= 40)
        or (research_score is not None and research_score >= 40)
        or (score is not None and score >= 50)
    )
    if promising and failed:
        history = record.setdefault("buy_gate_history", [])
        current_at = now.isoformat()
        if not history or history[-1].get("at") != current_at:
            history.append({
                "at": current_at,
                "stage": stage,
                "score": score,
                "research_score": research_score,
                "readiness_pct": percent,
                "passed_count": int(readiness.get("passed") or 0),
                "total_gates": int(readiness.get("total") or len(checks)),
                "failed_gate_keys": failed,
                "missing_conditions": list(readiness.get("missing_conditions") or [])[:12],
                "price": _safe_float(item.get("latest_price") or item.get("price")),
                "price_as_of": item.get("price_as_of"),
                "price_verified": bool(item.get("price_validation_passed")) or str(item.get("price_validation_status") or "").lower() == "verified",
                "market_regime": item.get("market_regime"),
                "max_favorable_since_pct": 0.0,
                "max_adverse_since_pct": 0.0,
                "latest_return_since_pct": 0.0,
                "follow_up_observations": 0,
            })
        record["buy_gate_history"] = history[-50:]

    # Later fresh observations update the outcome of earlier failed-gate snapshots.
    current_price = _safe_float(item.get("latest_price") or item.get("price"))
    current_as_of = _as_utc(item.get("price_as_of"))
    if observation_eligible and current_price and current_price > 0 and current_as_of:
        for event in record.get("buy_gate_history") or []:
            baseline = _safe_float(event.get("price"))
            baseline_at = _as_utc(event.get("price_as_of"))
            if not baseline or baseline <= 0 or not baseline_at or current_as_of <= baseline_at:
                continue
            change = (current_price / baseline - 1.0) * 100.0
            event["max_favorable_since_pct"] = round(max(_safe_float(event.get("max_favorable_since_pct")) or 0.0, change), 3)
            event["max_adverse_since_pct"] = round(min(_safe_float(event.get("max_adverse_since_pct")) or 0.0, change), 3)
            event["latest_return_since_pct"] = round(change, 3)
            event["follow_up_observations"] = int(event.get("follow_up_observations") or 0) + 1
            event["last_follow_up_at"] = now.isoformat()

    labels = item.get("catalyst_labels") or []
    flags = item.get("dilution_flags") or []
    if isinstance(labels, str):
        labels = [labels]
    if isinstance(flags, str):
        flags = [flags]
    labels = sorted({str(x).strip() for x in labels if str(x).strip()})
    flags = sorted({str(x).strip() for x in flags if str(x).strip()})
    status = (
        "CATALYST + DILUTION RISK" if labels and flags else
        "DILUTION RISK ACTIVE" if flags else
        "CATALYST ACTIVE" if labels else
        "NO CURRENT CLASSIFIED CATALYST"
    )
    fingerprint = json.dumps({"labels": labels, "flags": flags, "status": item.get("catalyst_dilution_status")}, sort_keys=True)
    if fingerprint != record.get("catalyst_lifecycle_fingerprint"):
        history = record.setdefault("catalyst_dilution_history", [])
        history.append({
            "at": now.isoformat(),
            "price_as_of": item.get("price_as_of"),
            "status": status,
            "provider_status": item.get("catalyst_dilution_status"),
            "catalyst_labels": labels[:12],
            "dilution_flags": flags[:12],
            "headline_count": int(item.get("catalyst_headline_count") or 0),
        })
        record["catalyst_dilution_history"] = history[-40:]
        record["catalyst_lifecycle_fingerprint"] = fingerprint
    record["catalyst_lifecycle_status"] = status
