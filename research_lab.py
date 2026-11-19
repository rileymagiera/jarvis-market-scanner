"""Isolated, PAPER_ONLY Research Lab ledger. Never imports or mutates Jarvis Core state."""
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("research_lab_state.json")
STARTING_CASH = 250.0
SCHEMA_VERSION = 1
PRICE_FRESHNESS_SECONDS = 30 * 60
MAX_RECORDS = 2000
_LOCK = threading.RLock()


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _now_utc(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _default_account():
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "PAPER_ONLY",
        "account": "research_lab",
        "enabled": False,
        "starting_cash": STARTING_CASH,
        "cash": STARTING_CASH,
        "positions": [],
        "closed_trades": [],
        "ledger": [],
        "processed_scan_ids": [],
        "processed_signal_ids": [],
        "equity_peak": STARTING_CASH,
        "created_at": None,
        "last_updated_at": None,
    }


def load_account(state_path=None):
    path = Path(state_path or STATE_PATH)
    if not path.exists():
        return _default_account()
    try:
        account = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Research Lab state is unreadable; refusing to reset it") from exc
    if not isinstance(account, dict) or account.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported Research Lab state schema; refusing to reset it")
    if account.get("mode") != "PAPER_ONLY" or account.get("account") != "research_lab":
        raise ValueError("Research Lab state identity/mode check failed")
    for key, default in _default_account().items():
        account.setdefault(key, default)
    return account


def save_account(account, state_path=None):
    path = Path(state_path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(account, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with _LOCK:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _verified_price(candidate, now):
    if not isinstance(candidate, dict) or candidate.get("price_validation_passed") is not True:
        return None
    price = _number(candidate.get("price"))
    as_of = _timestamp(candidate.get("price_as_of"))
    if price <= 0 or as_of is None:
        return None
    age = (now - as_of).total_seconds()
    if age < -30 or age > PRICE_FRESHNESS_SECONDS:
        return None
    return price


def _event(account, event_type, message, **fields):
    account["ledger"].append({"type": event_type, "message": message, **fields})
    account["ledger"] = account["ledger"][-MAX_RECORDS:]


def _cost_rate(signal, field):
    value = signal.get(field)
    if value is None:
        return None
    rate = _number(value, -1.0)
    if rate < 0 or rate > 0.05:
        return None
    return rate


def _exit_quantity(account, position, observed_price, now, fraction, reason):
    quantity = _number(position.get("quantity")) * fraction
    if quantity <= 0:
        return
    slippage = _number(position.get("slippage_rate"))
    fee_rate = _number(position.get("fee_rate"))
    exit_price = observed_price * (1.0 - slippage)
    gross = quantity * exit_price
    fee = gross * fee_rate
    net = gross - fee
    entry_cost = quantity * _number(position.get("entry_price"))
    entry_fee = quantity * _number(position.get("entry_fee_per_unit"))
    realized = net - entry_cost - entry_fee
    account["cash"] = round(_number(account.get("cash")) + net, 8)
    position["quantity"] = round(_number(position.get("quantity")) - quantity, 12)
    position["realized_pnl"] = round(_number(position.get("realized_pnl")) + realized, 8)
    position["fees_paid"] = round(_number(position.get("fees_paid")) + fee, 8)
    _event(account, "SELL", reason, at=now.isoformat(), symbol=position["symbol"],
           signal_id=position["signal_id"], price=round(exit_price, 10),
           quantity=round(quantity, 12), fee=round(fee, 8),
           realized_pnl=round(realized, 8))
    if position["quantity"] <= 1e-10:
        closed = dict(position)
        closed.update({"quantity": 0.0, "status": "closed", "closed_at": now.isoformat(),
                       "close_reason": reason, "close_price": round(exit_price, 10)})
        account["closed_trades"].append(closed)
        return True
    return False


def process_scan(candidates, experimental_signals, now, run_id, state_path=None, enabled=False):
    """Process explicitly tagged Lab signals only; disabled by default and idempotent by scan ID."""
    now = _now_utc(now)
    account = load_account(state_path)
    if not enabled or account.get("enabled") is not True:
        return account
    if not run_id or str(run_id) in set(account.get("processed_scan_ids") or []):
        return account

    candidate_map = {
        (str(item.get("asset_type") or "").lower(), str(item.get("symbol") or "").upper()): item
        for item in candidates if isinstance(item, dict)
    }

    remaining = []
    for position in account["positions"]:
        key = (str(position.get("asset_type") or "").lower(), str(position.get("symbol") or "").upper())
        candidate = candidate_map.get(key)
        price = _verified_price(candidate, now)
        if price is None:
            remaining.append(position)
            continue
        position["latest_price"] = price
        position["latest_price_as_of"] = candidate.get("price_as_of")
        target_1 = _number(position.get("target_1_price"))
        target_2 = _number(position.get("target_price"))
        stop = _number(position.get("stop_loss"))
        closed = False
        if stop > 0 and price <= stop:
            closed = _exit_quantity(account, position, price, now, 1.0, "STOP")
        elif target_2 > 0 and price >= target_2:
            closed = _exit_quantity(account, position, price, now, 1.0, "TARGET")
        elif target_1 > 0 and price >= target_1 and not position.get("target_1_filled"):
            fraction = min(1.0, max(0.0, _number(position.get("target_1_fraction"), 0.5)))
            closed = _exit_quantity(account, position, price, now, fraction, "TARGET_1_PARTIAL")
            position["target_1_filled"] = True
        if not closed:
            remaining.append(position)
    account["positions"] = remaining

    processed = set(account.get("processed_signal_ids") or [])
    for signal in experimental_signals:
        if not isinstance(signal, dict) or signal.get("experimental") is not True:
            continue
        signal_id = str(signal.get("signal_id") or "")
        if not signal_id or signal_id in processed or signal.get("status", "active") != "active":
            continue
        asset_type = str(signal.get("asset_type") or "").lower()
        symbol = str(signal.get("symbol") or "").upper()
        candidate = candidate_map.get((asset_type, symbol))
        price = _verified_price(candidate, now)
        if price is None:
            continue
        low, high = _number(signal.get("entry_low")), _number(signal.get("entry_high"))
        stop, target = _number(signal.get("stop_loss")), _number(signal.get("target_price"))
        notional = _number(signal.get("lab_notional"))
        fee_rate, slippage_rate = _cost_rate(signal, "fee_rate"), _cost_rate(signal, "slippage_rate")
        if not (low > 0 and high >= low and low <= price <= high and 0 < stop < price < target
                and notional >= 1 and fee_rate is not None and slippage_rate is not None
                and asset_type in ("stock", "crypto")):
            continue
        if any(p.get("symbol") == symbol and p.get("asset_type") == asset_type for p in account["positions"]):
            continue
        effective_entry = price * (1.0 + slippage_rate)
        fee = notional * fee_rate
        total_cost = notional + fee
        if total_cost > _number(account.get("cash")):
            _event(account, "REJECT", "Insufficient Research Lab cash", at=now.isoformat(),
                   symbol=symbol, signal_id=signal_id, requested_cost=round(total_cost, 8))
            continue
        quantity = notional / effective_entry
        position = {
            "signal_id": signal_id, "symbol": symbol, "asset_type": asset_type,
            "strategy_id": str(signal.get("strategy_id") or "unspecified"),
            "strategy_version": str(signal.get("strategy_version") or "1"),
            "experimental": True, "status": "open", "opened_at": now.isoformat(),
            "entry_price": effective_entry, "latest_price": price, "quantity": quantity,
            "initial_quantity": quantity, "notional": notional, "fee_rate": fee_rate,
            "slippage_rate": slippage_rate, "entry_fee_per_unit": fee / quantity,
            "entry_fee": fee, "fees_paid": fee, "stop_loss": stop,
            "target_1_price": _number(signal.get("target_1_price")),
            "target_1_fraction": _number(signal.get("target_1_fraction"), 0.5),
            "target_1_filled": False, "target_price": target,
            "market_regime": signal.get("market_regime") or "UNKNOWN",
            "price_source": candidate.get("price_source"),
            "entry_price_as_of": candidate.get("price_as_of"),
            "feature_snapshot": signal.get("feature_snapshot") if isinstance(signal.get("feature_snapshot"), dict) else {},
            "realized_pnl": 0.0,
        }
        account["cash"] = round(_number(account.get("cash")) - total_cost, 8)
        account["positions"].append(position)
        processed.add(signal_id)
        _event(account, "BUY", "Research Lab experimental paper entry", at=now.isoformat(),
               symbol=symbol, signal_id=signal_id, price=round(effective_entry, 10),
               quantity=round(quantity, 12), notional=round(notional, 8), fee=round(fee, 8),
               strategy_id=position["strategy_id"], strategy_version=position["strategy_version"])

    account["processed_signal_ids"] = list(processed)[-MAX_RECORDS:]
    account["processed_scan_ids"] = (account.get("processed_scan_ids") or []) + [str(run_id)]
    account["processed_scan_ids"] = account["processed_scan_ids"][-MAX_RECORDS:]
    account["closed_trades"] = account["closed_trades"][-MAX_RECORDS:]
    equity = _number(account["cash"]) + sum(
        _number(p.get("quantity")) * _number(p.get("latest_price") or p.get("entry_price"))
        for p in account["positions"]
    )
    account["equity_peak"] = max(_number(account.get("equity_peak"), STARTING_CASH), equity)
    account["last_updated_at"] = now.isoformat()
    if not account.get("created_at"):
        account["created_at"] = now.isoformat()
    save_account(account, state_path)
    return account


def account_summary(account):
    positions = account.get("positions") or []
    market_value = sum(_number(p.get("quantity")) * _number(p.get("latest_price") or p.get("entry_price"))
                       for p in positions)
    realized = sum(_number(t.get("realized_pnl")) for t in account.get("closed_trades", []))
    realized += sum(_number(p.get("realized_pnl")) for p in positions)
    unrealized = sum(
        _number(p.get("quantity")) * (_number(p.get("latest_price") or p.get("entry_price"))
                                      - _number(p.get("entry_price")))
        - _number(p.get("quantity")) * _number(p.get("entry_fee_per_unit"))
        for p in positions
    )
    equity = _number(account.get("cash")) + market_value
    peak = max(_number(account.get("equity_peak"), STARTING_CASH), equity)
    return {
        "account": "research_lab", "mode": "PAPER_ONLY",
        "enabled": account.get("enabled") is True,
        "starting_cash": _number(account.get("starting_cash"), STARTING_CASH),
        "cash": round(_number(account.get("cash")), 8),
        "market_value": round(market_value, 8),
        "equity": round(equity, 8),
        "realized_pnl": round(realized, 8),
        "unrealized_pnl": round(unrealized, 8),
        "total_return_pct": round((equity / STARTING_CASH - 1) * 100, 4),
        "drawdown_pct": round((equity / peak - 1) * 100, 4) if peak else 0.0,
        "open_positions": len(positions),
        "closed_trades": len(account.get("closed_trades") or []),
        "fees_paid": round(sum(_number(p.get("fees_paid")) for p in positions)
                           + sum(_number(t.get("fees_paid")) for t in account.get("closed_trades", [])), 8),
    }
