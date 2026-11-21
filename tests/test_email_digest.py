"""Notification-only regression tests: paper execution and scan logic unchanged."""
from copy import deepcopy
from datetime import datetime, timezone
from email_digest import (
    digest_due, format_daily_digest, mark_digest_sent, queue_buy_digest_signals,
    queue_paper_digest_events, queue_research_digest,
)


def _time(day=20, hour=20):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def _watch(symbol, score=77):
    return {"asset_type": "stock", "symbol": symbol, "stage": "EARLY WATCH",
            "score": score, "price": 3.4, "news": [{"title": "Catalyst"}],
            "data_quality": {"data_type": "live"},
            "price_validation_status": "verified"}


def test_queue_deduplicates_watch_without_mutating_candidates():
    item = _watch("TEST")
    snapshot = deepcopy(item)
    state = {}
    queue_research_digest(state, [item], [])
    queue_research_digest(state, [item], [])
    assert item == snapshot
    assert len(state["email_digest_pending"]["watch"]) == 1


def test_premarket_buy_and_paper_count_are_distinct():
    state = {}
    queue_research_digest(state, [], [{"symbol": "ABCD", "premarket_alert_score": 90}])
    queue_buy_digest_signals(state, [{"asset_type": "crypto", "symbol": "SOLUSD", "stage": "BUY", "score": 90}])
    queue_paper_digest_events(state, [{"type": "BUY"}, {"type": "SELL"}, {"type": "SELL"}])
    assert "ABCD" in state["email_digest_pending"]["premarket"]
    assert "crypto:SOLUSD" in state["email_digest_pending"]["buy_signals"]
    assert state["email_digest_pending"]["paper_events"] == {"BUY": 1, "SELL": 2}


def test_digest_once_during_eight_pm_hour():
    state = {}
    assert not digest_due(_time(hour=19), state)
    assert digest_due(_time(), state)
    mark_digest_sent(state, _time())
    assert not digest_due(_time(), state)
    assert state["email_digest_pending"] == {}
    assert digest_due(_time(day=21), state)


def test_digest_contains_watch_paper_and_health():
    state = {}
    queue_research_digest(state, [_watch("TEST")], [])
    queue_paper_digest_events(state, [{"type": "BUY"}])
    account = {"equity": 258.1, "total_return_pct": 3.24, "open_positions": 1,
               "realized_pnl": -2.3, "unrealized_pnl": 7.7}
    subject, body = format_daily_digest(
        state, _time(), {"regime": "RISK-OFF"}, 125, 2, _time().isoformat(), account)
    assert "DAILY DIGEST" in subject
    assert "TEST" in body
    assert "$258.10" in body
    assert "1 bought / 0 sold" in body
    assert "Last scan issues: 2" in body


def test_queue_bounds_top_score():
    state = {}
    queue_research_digest(state, [_watch("STK" + str(i), i) for i in range(140)], [])
    saved = state["email_digest_pending"]["watch"]
    assert len(saved) == 80
    assert "stock:STK139" in saved
    assert "stock:STK0" not in saved
