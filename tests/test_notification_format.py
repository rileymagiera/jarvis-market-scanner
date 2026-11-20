"""Email clarity contracts. No market strategy or paper ledger mutation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from notification_format import (
    current_paper_events, event_fingerprint, format_daily_status,
    format_market_digest, format_paper_trades, format_premarket_watch,
    mark_paper_events_notified,
)


NOW = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)


def _event(kind, symbol="MEGAUSDT", at=None):
    row = {
        "at": at or NOW.isoformat(), "type": kind, "symbol": symbol,
        "asset_type": "crypto", "quantity": 42.5, "price": 0.05975,
        "fee": 0.03, "signal_id": "risk_off:crypto:MEGAUSDT:test",
    }
    if kind == "BUY":
        row.update({"strategy": "risk_off_experimental", "experimental": True,
                    "notional": 2.539375})
    else:
        row.update({"realized_pnl": -2.14, "message": "STOP observed for MEGAUSDT"})
    return row


def test_paper_buy_says_bought_but_only_in_simulation():
    e = _event("BUY")
    account = {"ledger": [e], "positions": [{
        "signal_id": e["signal_id"], "stop_loss": 0.0578, "target_1": 0.064, "target_2": 0.07,
    }], "closed_trades": []}
    copy = deepcopy(account)
    subject, message = format_paper_trades([e], account)
    assert subject == "Jarvis PAPER BOUGHT: MEGAUSDT"
    assert "PAPER BOUGHT: MEGAUSDT @ $0.05975" in message
    assert "Quantity: 42.5" in message
    assert "Stop: $0.0578" in message
    assert "Target 1: $0.064" in message
    assert "EXPERIMENTAL" in message
    assert "SIMULATED TRADES ONLY" in message
    assert account == copy


def test_paper_sell_reports_cost_adjusted_negative_pnl():
    e = _event("SELL")
    subject, body = format_paper_trades([e], {"positions": [], "closed_trades": []})
    assert "PAPER SOLD: MEGAUSDT" in subject
    assert "P/L: $-2.14" in subject
    assert "Realized paper P/L after modeled costs: $-2.14" in body
    assert "Reason: STOP" in body
    assert "CLOSED" in body
    assert "No real" in body


def test_partial_sale_is_labeled_partial_and_multi_event_subject():
    one = _event("BUY")
    two = _event("SELL")
    account = {"positions": [{"signal_id": two["signal_id"]}], "closed_trades": []}
    subject, body = format_paper_trades([one, two], account)
    assert subject == "Jarvis PAPER TRADES: 1 bought, 1 sold"
    assert "PARTIAL SALE" in body


def test_older_ledger_events_not_replayed_on_first_rollout_and_next_scan_is_queued():
    old = _event("BUY", at=(NOW - timedelta(minutes=15)).isoformat())
    new = _event("SELL")
    state = {}
    account = {"ledger": [old, new]}
    original = deepcopy(account)
    pending = current_paper_events(account, state, NOW)
    assert pending == [new]
    assert len(state["paper_email_seen_events"]) == 1
    # If outside the existing email window, pending is not lost on the next scan.
    assert current_paper_events(account, state, NOW + timedelta(minutes=15)) == [new]
    mark_paper_events_notified(state, pending)
    assert current_paper_events(account, state, NOW + timedelta(minutes=30)) == []
    assert account == original
    assert event_fingerprint(old) != event_fingerprint(new)


def test_market_watch_never_claims_a_trade_was_made():
    selected = [
        {"asset_type": "stock", "symbol": "NUAI", "stage": "DEVELOPING RUNNER",
         "score": 81, "price": 4.26, "price_as_of": NOW.isoformat(),
         "price_validation_status": "last_known",
         "data_quality": {"data_type": "EOD"},
         "levels": {"watch_entry_low": 4.26, "watch_entry_high": 4.39},
         "news": [{"title": "Filing reviewed"}]},
        {"asset_type": "crypto", "symbol": "SOLUSDT", "stage": "BUY",
         "score": 93, "price": 145, "price_as_of": NOW.isoformat(),
         "price_validation_status": "verified", "data_quality": {"data_type": "live"},
         "levels": {"watch_entry_low": 143, "watch_entry_high": 146,
                    "stop_loss": 138, "target_1": 158, "target_2": 170}},
    ]
    subject, body = format_market_digest(selected, [], {"regime": "RISK-OFF"}, 238, 1, NOW.isoformat())
    assert "BUY SIGNAL" in subject
    assert "NOT EXECUTED: SOLUSDT" in body
    assert "WATCH ONLY — NOT BOUGHT: NUAI" in body
    assert "Price Target 1: $158" in body
    assert "Suggested entry: $143–$146" in body
    assert "Entry: WAIT" in body
    assert "last_known" in body
    assert "Filing reviewed" in body
    assert "NO REAL" not in body.upper() or "No real orders" in body
    assert "Scan issues: 1" in body


def test_premarket_and_heartbeat_short_and_explicit():
    subject, body = format_premarket_watch(
        [{"symbol": "TEST", "price": 1.5, "price_as_of": NOW.isoformat(),
          "premarket_alert_status": "PREMARKET PROVISIONAL"}], NOW.isoformat())
    assert "WATCH ONLY" in subject
    assert "NOT BOUGHT" in body
    assert "PREMARKET PROVISIONAL" in body
    daily = format_daily_status({"regime": "RISK-OFF"}, 238, 1, NOW.isoformat())
    assert "NO NEW BUY SIGNALS" in daily
    assert "Scanner issues: 1" in daily


def test_no_events_returns_none_and_handles_missing_optional_fields():
    assert format_paper_trades([], {}) is None
    assert format_paper_trades([{"type": "BUY", "symbol": "TEST"}], {})[0] == "Jarvis PAPER BOUGHT: TEST"
