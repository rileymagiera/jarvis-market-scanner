"""Deterministic, offline-only paper trading for verified official Jarvis BUY signals."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("paper_trading_state.json")
STARTING_CASH = 250.0
# Coinbase One Basic: eligible simple app trades have $0 trading commission up to $500 monthly volume.
# App quotes include a variable spread; use a labeled 0.50% per-side estimate until a trade-preview spread is supplied.
COINBASE_ONE_MONTHLY_CAP = 500.0
COINBASE_APP_SPREAD_ESTIMATE_RATE = 0.005
# Webull U.S.-listed stock pass-through fees from its published pricing schedule.
WEBULL_COMMISSION_RATE = 0.0
WEBULL_CAT_RATE = 0.000003
WEBULL_SEC_SELL_RATE = 0.0000206
WEBULL_FINRA_TAF_PER_SHARE = 0.0
DEFAULT_CRYPTO_ORDER_TYPE = "simple_app"
MAX_OPEN_POSITIONS = 4
MAX_POSITION_FRACTION = 0.25
RISK_FRACTION = 0.01


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def calculate_trade_fee(asset_type, side, notional, quantity=0.0, order_type=None):
    """Estimate Coinbase app spread or Webull U.S. stock regulatory pass-through fees."""
    asset = str(asset_type or "").lower()
    side = str(side or "").lower()
    notional = max(0.0, _num(notional))
    quantity = max(0.0, _num(quantity))
    if asset in ("crypto", "cryptocurrency"):
        # Coinbase One Basic waives eligible simple-app trading commission within
        # its $500 monthly cap. Spread varies and is visible in each trade preview.
        spread = notional * COINBASE_APP_SPREAD_ESTIMATE_RATE
        return {"broker": "Coinbase app + Coinbase One Basic",
                "order_type": "simple_app", "commission": 0.0,
                "estimated_spread_cost": round(spread, 8),
                "spread_estimate_rate": COINBASE_APP_SPREAD_ESTIMATE_RATE,
                "monthly_zero_fee_cap": COINBASE_ONE_MONTHLY_CAP,
                "total": round(spread, 8), "rate": COINBASE_APP_SPREAD_ESTIMATE_RATE,
                "note": "Estimated spread only; actual spread varies by asset, size and market conditions. The $500 monthly Coinbase One cap is not tracked; standard fees may apply after it is exceeded"}
    if asset in ("stock", "stocks", "equity", "etf"):
        commission = notional * WEBULL_COMMISSION_RATE
        cat = notional * WEBULL_CAT_RATE
        sec = notional * WEBULL_SEC_SELL_RATE if side == "sell" else 0.0
        finra = quantity * WEBULL_FINRA_TAF_PER_SHARE if side == "sell" else 0.0
        total = commission + cat + sec + finra
        return {"broker": "Webull", "order_type": "stock", "commission": round(commission, 8),
                "cat_fee": round(cat, 8), "sec_fee": round(sec, 8),
                "finra_taf": round(finra, 8), "regulatory": round(cat + sec + finra, 8),
                "total": round(total, 8), "rate": (total / notional if notional else 0.0)}
    return {"broker": "Unspecified", "order_type": "unknown", "commission": 0.0,
            "regulatory": 0.0, "total": 0.0, "rate": 0.0}


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
            state.setdefault("rules", {})
            state["rules"].update({
                "crypto_fee_schedule": "Coinbase One Basic app: zero eligible trading commission up to $500/month; estimated spread 0.50% per side; simulated monthly gross volume tracked; standard commission after $500 cap not configured",
                "stock_fee_schedule": "Webull US stocks: $0 commission; CAT 0.0003% per side; SEC 0.00206% sell side; FINRA TAF currently $0",
                "crypto_default_order_type": DEFAULT_CRYPTO_ORDER_TYPE,
            })
            for position in state.get("positions", []):
                if isinstance(position, dict):
                    quantity = _num(position.get("quantity"))
                    price = _num(position.get("latest_price") or position.get("entry_price"))
                    position["estimated_exit_fee"] = calculate_trade_fee(
                        position.get("asset_type"), "sell", quantity * price, quantity
                    )["total"]
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
            "max_open_positions": MAX_OPEN_POSITIONS, "crypto_fee_schedule": "Coinbase One Basic app: zero eligible trading commission up to $500/month; estimated spread 0.50% per side; simulated monthly gross volume tracked; standard commission after $500 cap not configured",
            "stock_fee_schedule": "Webull US stocks: $0 commission; CAT 0.0003% per side; SEC 0.00206% sell side; FINRA TAF currently $0",
            "exits": "50% at target 1, remainder at target 2; full exit at stop",
        },
    }



def _record_coinbase_monthly_volume(account, notional, now):
    """Track gross Coinbase app buy/sell notional against the monthly One allowance."""
    month = now.strftime("%Y-%m")
    volumes = account.setdefault("coinbase_one_monthly_volume", {})
    before = _num(volumes.get(month))
    amount = max(0.0, _num(notional))
    eligible = min(amount, max(0.0, COINBASE_ONE_MONTHLY_CAP - before))
    over_cap = max(0.0, amount - eligible)
    after = before + amount
    volumes[month] = round(after, 8)
    for old_month in sorted(volumes)[:-12]:
        volumes.pop(old_month, None)
    return {
        "month": month, "volume_before": round(before, 8),
        "trade_volume": round(amount, 8), "eligible_volume": round(eligible, 8),
        "volume_over_cap": round(over_cap, 8),
        "remaining_allowance": round(max(0.0, COINBASE_ONE_MONTHLY_CAP - after), 8),
        "monthly_volume_after": round(after, 8),
        "standard_commission_status": "rate_not_configured" if over_cap > 0 else "within_included_volume",
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
        position["estimated_exit_fee"] = calculate_trade_fee(
            position["asset_type"], "sell", qty * price, qty
        )["total"]
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
            fee_info = calculate_trade_fee(position["asset_type"], "sell", gross, sell_qty)
            fee = fee_info["total"]
            net = gross - fee
            coinbase_volume = (_record_coinbase_monthly_volume(account, gross, now)
                               if position["asset_type"] in ("crypto", "cryptocurrency") else None)
            account["cash"] = round(_num(account["cash"]) + net, 8)
            entry_cost = sell_qty * _num(position.get("entry_price"))
            entry_fee_allocated = min(_num(position.get("entry_fee_remaining")),
                                      _num(position.get("entry_fee")) * (sell_qty / max(_num(position.get("initial_quantity")), 1e-12)))
            position["entry_fee_remaining"] = max(0.0, _num(position.get("entry_fee_remaining")) - entry_fee_allocated)
            realized = net - entry_cost - entry_fee_allocated
            position["fees_paid"] = round(_num(position.get("fees_paid")) + fee, 8)
            position["realized_pnl"] = round(_num(position.get("realized_pnl")) + realized, 8)
            position["quantity"] = round(qty - sell_qty, 10)
            position["estimated_exit_fee"] = calculate_trade_fee(
                position["asset_type"], "sell",
                position["quantity"] * price, position["quantity"]
            )["total"]
            _event(account, "SELL", f'{exit_reason} observed for {position["symbol"]}', symbol=position["symbol"],
                   asset_type=position["asset_type"], price=price, quantity=sell_qty,
                   fee=round(fee, 8), fee_breakdown=fee_info, entry_fee_allocated=round(entry_fee_allocated, 8),
                   realized_pnl=round(realized, 8), coinbase_one_volume=coinbase_volume,
                   signal_id=position["signal_id"])
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
    eligible = []
    allocation_review = []
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
            _event(account, "SKIP", "{} BUY signal skipped: invalid risk levels".format(signal.get("symbol")),
                   symbol=signal.get("symbol"), signal_id=signal_id)
            continue
        score = _num(signal.get("score"))
        reward_risk = _num(signal.get("target_2_reward_risk_ratio") or signal.get("reward_risk_ratio"))
        if reward_risk <= 0:
            reward_risk = (target2 - price) / (price - stop)
        eligible.append({
            "signal": signal, "signal_id": signal_id, "key": key, "item": item,
            "price": price, "stop": stop, "target1": target1, "target2": target2,
            "score": score, "reward_risk": reward_risk,
        })

    # Rank only already-active official BUY signals. The existing BUY gates remain upstream.
    # Existing official score is primary; target-2 reward/risk breaks ties; ID makes ties stable.
    eligible.sort(key=lambda row: (-row["score"], -row["reward_risk"], row["signal_id"]))
    for rank, row in enumerate(eligible, start=1):
        signal, signal_id = row["signal"], row["signal_id"]
        key, item = row["key"], row["item"]
        price, stop = row["price"], row["stop"]
        target1, target2 = row["target1"], row["target2"]
        review = {
            "rank": rank, "signal_id": signal_id, "symbol": key[1],
            "asset_type": key[0], "official_score": row["score"],
            "target_2_reward_risk": round(row["reward_risk"], 4),
        }
        if len(account["positions"]) >= MAX_OPEN_POSITIONS:
            review.update({"status": "deferred", "reason": "Four-position limit; will be reconsidered while BUY remains active"})
            allocation_review.append(review)
            continue
        equity = _num(account["cash"]) + sum(_num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")) for p in account["positions"])
        risk_budget = equity * RISK_FRACTION
        entry_fee_per_unit = calculate_trade_fee(key[0], "buy", price, 1.0)["total"]
        stop_exit_fee_per_unit = calculate_trade_fee(key[0], "sell", stop, 1.0)["total"]
        risk_per_unit = (price - stop) + entry_fee_per_unit + stop_exit_fee_per_unit
        qty_by_risk = risk_budget / risk_per_unit if risk_per_unit > 0 else 0.0
        qty_by_cap = equity * MAX_POSITION_FRACTION / price
        cash = _num(account["cash"])
        quantity = round(max(0.0, min(qty_by_risk, qty_by_cap, cash / price)), 10)
        for _ in range(3):
            entry_fee_info = calculate_trade_fee(key[0], "buy", quantity * price, quantity)
            total_cost = quantity * price + entry_fee_info["total"]
            if total_cost <= cash or quantity <= 0:
                break
            quantity = round(quantity * (cash / total_cost), 10)
        notional = quantity * price
        entry_fee_info = calculate_trade_fee(key[0], "buy", notional, quantity)
        fee = entry_fee_info["total"]
        if quantity <= 0 or notional < 1.0:
            review.update({"status": "deferred", "reason": "Insufficient available paper cash; will be reconsidered"})
            allocation_review.append(review)
            continue
        coinbase_volume = (_record_coinbase_monthly_volume(account, notional, now)
                           if key[0] in ("crypto", "cryptocurrency") else None)
        account["cash"] = round(_num(account["cash"]) - notional - fee, 8)
        position = {
            "signal_id": signal_id, "symbol": key[1], "asset_type": key[0],
            "opened_at": now.isoformat(), "entry_price": price, "latest_price": price,
            "latest_price_as_of": item.get("price_as_of"), "quantity": quantity,
            "initial_quantity": quantity, "notional": round(notional, 8), "entry_fee": round(fee, 8),
            "entry_fee_remaining": round(fee, 8), "fees_paid": round(fee, 8),
            "estimated_exit_fee": calculate_trade_fee(key[0], "sell", notional, quantity)["total"],
            "entry_fee_breakdown": entry_fee_info,
            "stop_loss": stop, "target_1": target1, "target_2": target2,
            "score": signal.get("score"), "score_model": signal.get("score_model"),
            "allocation_rank": rank, "allocation_reward_risk": round(row["reward_risk"], 4),
            "price_source": item.get("price_source") or item.get("data_source"),
            "entry_price_as_of": item.get("price_as_of"), "target_1_filled": False,
            "realized_pnl": 0.0, "status": "open",
        }
        account["positions"].append(position)
        processed.add(signal_id)
        review.update({"status": "allocated", "reason": "Highest-ranked eligible setup within current risk and capacity limits", "paper_notional": round(notional, 2)})
        allocation_review.append(review)
        _event(account, "BUY", "Auto paper BUY: {}".format(key[1]), symbol=key[1], asset_type=key[0],
               price=price, quantity=quantity, notional=round(notional, 8), fee=round(fee, 8),
               allocation_rank=rank, official_score=row["score"], target_2_reward_risk=round(row["reward_risk"], 4),
               coinbase_one_volume=coinbase_volume, signal_id=signal_id)
    account["allocation_review"] = allocation_review
    account["processed_signal_ids"] = list(processed)[-2000:]
    account["closed_trades"] = account["closed_trades"][-500:]
    account["ledger"] = account["ledger"][-500:]
    account["cash"] = round(_num(account["cash"]), 8)
    STATE_PATH.write_text(json.dumps(account, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return account


def account_summary(account, now=None):
    positions = account.get("positions") or []
    market_value = sum(_num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")) for p in positions)
    unrealized = sum(
        _num(p.get("quantity")) * (_num(p.get("latest_price") or p.get("entry_price")) - _num(p.get("entry_price")))
        - _num(p.get("entry_fee_remaining"))
        - calculate_trade_fee(
            p.get("asset_type"), "sell",
            _num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")),
            _num(p.get("quantity"))
        )["total"]
        for p in positions
    )
    realized = sum(_num(t.get("realized_pnl")) for t in (account.get("closed_trades") or []))
    realized += sum(_num(p.get("realized_pnl")) for p in positions)
    estimated_exit_fees = sum(
        calculate_trade_fee(
            p.get("asset_type"), "sell",
            _num(p.get("quantity")) * _num(p.get("latest_price") or p.get("entry_price")),
            _num(p.get("quantity"))
        )["total"] for p in positions
    )
    equity = _num(account.get("cash")) + market_value - estimated_exit_fees
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    coinbase_volume = _num((account.get("coinbase_one_monthly_volume") or {}).get(month))
    return {
        "starting_cash": _num(account.get("starting_cash"), STARTING_CASH),
        "coinbase_one_monthly_volume": round(coinbase_volume, 2),
        "coinbase_one_remaining_allowance": round(max(0.0, COINBASE_ONE_MONTHLY_CAP - coinbase_volume), 2),
        "coinbase_one_allowance_status": "allowance_exceeded_rate_unconfigured" if coinbase_volume > COINBASE_ONE_MONTHLY_CAP else "within_included_volume",
        "cash": round(_num(account.get("cash")), 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "total_return_pct": round((equity / STARTING_CASH - 1) * 100, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "estimated_exit_fees": round(estimated_exit_fees, 2),
        "open_positions": len(positions),
        "closed_trades": len(account.get("closed_trades") or []),
    }
