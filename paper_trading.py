"""Deterministic, offline-only paper trading for verified official Jarvis BUY signals."""
import json
import math
from pathlib import Path

STATE_PATH = Path("paper_trading_state.json")
STARTING_CASH = 250.0
FEE_RATE = 0.001
MAX_OPEN_POSITIONS = 4
MAX_POSITION_FRACTION = 0.25
RISK_FRACTION = 0.01


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def load_account():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("version") == 1:
            state.setdefault("starting_cash", STARTING_CASH)
            state.setdefault("cash", STARTING_CASH)
            state.setdefault("positions", [])
            state.setdefault("closed_trades", [])
            state.setdefault("ledger", [])
            state.setdefault("processed_signal_ids", [])
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "version": 1, "mode": "PAPER_ONLY", "starting_cash": STARTING_CASH,
        "cash": STARTING_CASH, "positions": [], "closed_trades": [],
        "ledger": [], "processed_signal_ids": [], "created_at": None,
        "last_updated_at": None, "rules": {
            "entry": "Official BUY signal + verified price inside saved entry range",
            "risk_per_trade_pct": 1.0, "max_position_pct": 25.0,
            "max_open_positions": MAX_OPEN_POSITIONS, "fee_per_side_pct": FEE_RATE * 100,
            "exits": "50% at target 1, remainder at target 2; full exit at stop",
        },
    }


def _event(account, kind, message, **fields):
    account["ledger"].append({"at": account.get("last_updated_at"), "type": kind, "message": message, **fields})
    account["ledger"] = account["ledger"][-500:]


def update_account(candidates, buy_signals, now):
    """Process one completed scan; sampled exits are explicitly not tick-level fills."""
    account = load_account()
    account["last_updated_at"] = now.isoformat()
    if not account.get("created_at"):
        account["created_at"] = now.isoformat()
    candidate_by_key = {
        (str(x.get("asset_type") or ""), str(x.get("symbol") or "").upper()): x
        for x in candidates if isinstance(x, dict)
    }
    # Manage existing positions first, using only current independently verified prices.
    remaining = []
    for position in account["positions"]:
        item = candidate_by_key.get((position["asset_type"], position["symbol"]))
        if not item or item.get("price_validation_passed") is not True:
            remaining.append(position)
            continue
        price = _num(item.get("price"))
        if price <= 0:
            remaining.append(position)
            continue
        position["latest_price"] = price
        position["latest_price_as_of"] = item.get("price_as_of")
        position["latest_observed_at"] = now.isoformat()
        qty = _num(position.get("quantity"))
        stop = _num(position.get("stop_loss"))
        target1 = _num(position.get("target_1"))
        target2 = _num(position.get("target_2"))
        exit_reason = None
        exit_fraction = 0.0
        if stop > 0 and price <= stop:
            exit_reason, exit_fraction = "STOP", 1.0
        elif target2 > 0 and price >= target2:
            exit_reason, exit_fraction = "TARGET_2", 1.0
        elif target1 > 0 and price >= target1 and not position.get("target_1_filled"):
            exit_reason, exit_fraction = "TARGET_1", 0.5
        if exit_fraction:
            sell_qty = qty * exit_fraction
            gross = sell_qty * price
            fee = gross * FEE_RATE
            net = gross - fee
            account["cash"] = round(_num(account["cash"]) + net, 8)
            entry_cost = sell_qty * _num(position.get("entry_price"))
            realized = net - entry_cost - (entry_cost * FEE_RATE)
            position["realized_pnl"] = round(_num(position.get("realized_pnl")) + realized, 8)
            position["quantity"] = round(qty - sell_qty, 10)
            _event(account, "SELL", f'{exit_reason} observed for {position["symbol"]}', symbol=position["symbol"],
                   asset_type=position["asset_type"], price=price, quantity=sell_qty,
                   fee=round(fee, 8), realized_pnl=round(realized, 8), signal_id=position["signal_id"])
            if exit_reason == "TARGET_1":
                position["target_1_filled"] = True
                position["target_1_observed_at"] = now.isoformat()
            if position["quantity"] > 1e-10:
                remaining.append(position)
            else:
                account["closed_trades"].append({**position, "status": "closed", "closed_at": now.isoformat(),
                                                  "close_reason": exit_reason, "close_price": price})
        else:
            remaining.append(position)
    account["positions"] = remaining

    processed = set(account.get("processed_signal_ids") or [])
    active = [s for s in buy_signals if isinstance(s, dict) and s.get("status") == "active"]
    active.sort(key=lambda s: (s.get("at") or "", s.get("signal_id") or ""))
    for signal in active:
        signal_id = str(signal.get("signal_id") or "")
        if not signal_id or signal_id in processed:
            continue
        key = (str(signal.get("asset_type") or ""), str(signal.get("symbol") or "").upper())
        item = candidate_by_key.get(key)
        if not item or item.get("price_validation_passed") is not True:
            continue
        price = _num(item.get("price"))
        low = _num(signal.get("entry_low"))
        high = _num(signal.get("entry_high"))
        stop = _num(signal.get("stop_loss") or signal.get("invalidation"))
        target1 = _num(signal.get("target_1"))
        target2 = _num(signal.get("target_2"))
        if price <= 0 or low <= 0 or high < low or not (low <= price <= high):
            continue
        if not (0 < stop < price < target1 <= target2):
            processed.add(signal_id)
            _event(account, "SKIP", f'{signal.get("symbol")} BUY signal skipped: invalid risk levels',
                   symbol=signal.get("symbol"), signal_id=signal_id)
            continue
        if len(account["positions"]) >= MAX_OPEN_POSITIONS:
            processed.add(signal_id)
            _event(account, "SKIP", f'{signal.get("symbol")} BUY signal skipped: four-position limit reached',
                   symbol=signal.get("symbol"), signal_id=signal_id)
            continue
        equity = _num(account["cash"]) + sum(_num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")) for p in account["positions"])
        risk_budget = equity * RISK_FRACTION
        qty_by_risk = risk_budget / (price - stop)
        qty_by_cap = equity * MAX_POSITION_FRACTION / price
        qty_by_cash = _num(account["cash"]) / (price * (1 + FEE_RATE))
        quantity = round(max(0.0, min(qty_by_risk, qty_by_cap, qty_by_cash)), 10)
        notional = quantity * price
        fee = notional * FEE_RATE
        if quantity <= 0 or notional < 1.0:
            processed.add(signal_id)
            _event(account, "SKIP", f'{signal.get("symbol")} BUY signal skipped: insufficient available paper capital',
                   symbol=signal.get("symbol"), signal_id=signal_id)
            continue
        account["cash"] = round(_num(account["cash"]) - notional - fee, 8)
        position = {
            "signal_id": signal_id, "symbol": key[1], "asset_type": key[0],
            "opened_at": now.isoformat(), "entry_price": price, "latest_price": price,
            "latest_price_as_of": item.get("price_as_of"), "quantity": quantity,
            "initial_quantity": quantity, "notional": round(notional, 8), "entry_fee": round(fee, 8),
            "stop_loss": stop, "target_1": target1, "target_2": target2,
            "score": signal.get("score"), "score_model": signal.get("score_model"),
            "price_source": item.get("price_source") or item.get("data_source"),
            "entry_price_as_of": item.get("price_as_of"), "target_1_filled": False,
            "realized_pnl": 0.0, "status": "open",
        }
        account["positions"].append(position)
        processed.add(signal_id)
        _event(account, "BUY", f'Auto paper BUY: {key[1]}', symbol=key[1], asset_type=key[0],
               price=price, quantity=quantity, notional=round(notional, 8), fee=round(fee, 8),
               signal_id=signal_id)
    account["processed_signal_ids"] = list(processed)[-2000:]
    account["closed_trades"] = account["closed_trades"][-500:]
    account["ledger"] = account["ledger"][-500:]
    account["cash"] = round(_num(account["cash"]), 8)
    STATE_PATH.write_text(json.dumps(account, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return account


def account_summary(account):
    positions = account.get("positions") or []
    market_value = sum(_num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")) for p in positions)
    unrealized = sum(_num(p.get("quantity")) * (_num(p.get("latest_price") or p.get("entry_price")) - _num(p.get("entry_price"))) for p in positions)
    realized = sum(_num(t.get("realized_pnl")) for t in (account.get("closed_trades") or []))
    realized += sum(_num(p.get("realized_pnl")) for p in positions)
    equity = _num(account.get("cash")) + market_value
    return {
        "starting_cash": _num(account.get("starting_cash"), STARTING_CASH),
        "cash": round(_num(account.get("cash")), 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "total_return_pct": round((equity / STARTING_CASH - 1) * 100, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_positions": len(positions),
        "closed_trades": len(account.get("closed_trades") or []),
    }
