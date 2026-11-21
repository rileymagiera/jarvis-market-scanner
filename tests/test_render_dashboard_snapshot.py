"""The Render projection must never modify trading or research source data."""
import base64
import copy
import gzip
import json
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import main
import send_alerts
from dashboard_snapshot import build_render_snapshot, compact_paper_tracker
from runner_intelligence import build_runner_intelligence_report


def _tracker():
    return {
        "crypto:SOLUSDT": {
            "symbol": "SOLUSDT", "asset_type": "crypto",
            "currently_surfaced": True, "latest_price": 112.0,
            "price_validation_status": "verified",
            "discovery_initial_price": 91.18,
            "levels": {"watch_entry_low": 100.0, "stop_loss": 90.0,
                       "target_1": 125.0, "target_2": 135.0},
            "signal_history": [{"scan_id": str(i), "replay_levels": {"note": "x" * 800}}
                               for i in range(168)],
            "price_history": [{"price": i} for i in range(168)],
            "paired_entry_history": [{"entry": i} for i in range(100)],
            "buy_gate_history": [{"at": f"2026-11-20T00:{i:02d}:00Z", "score": 50,
                                  "failed_gate_keys": ["freshness"], "follow_up_observations": 1,
                                  "max_favorable_since_pct": 6.0,
                                  "max_adverse_since_pct": -3.0}
                                 for i in range(40)],
            "grade_history": [{"from": "EARLY WATCH", "to": "BUY", "at": "2026-11-20"}],
        },
        "stock:ARCHIVE": {
            "symbol": "ARCHIVE", "asset_type": "stock",
            "currently_surfaced": False,
            "signal_history": [{"scan_id": str(i)} for i in range(20)],
            "price_history": [{"price": i} for i in range(20)],
            "paired_entry_history": [{"entry": i} for i in range(20)],
            "grade_history": [{"from": "WATCH", "to": "LATE"}],
        },
    }


def _account():
    return {"version": 1, "cash": 100, "positions": [
        {"symbol": "SOLUSDT", "entry": 90, "stop": 80,
         "target_1": 110, "target_2": 120}], "events": [{"kind": "BUY"}]}


def test_render_projection_preserves_full_source_and_all_trade_levels():
    full = _tracker()
    original = copy.deepcopy(full)
    account = _account()
    signals = [{"symbol": "SOLUSDT", "stage": "BUY"}]
    snapshot = build_render_snapshot(
        full, {"recommendations": 17}, datetime.now(timezone.utc),
        paper_account=account, buy_signals=signals,
        scan_metadata={"run_id": "scan-1", "last_scan_completed_at": "2026-11-20T20:00:00Z"})
    current = snapshot["paper_tracker"]["crypto:SOLUSDT"]
    archived = snapshot["paper_tracker"]["stock:ARCHIVE"]
    assert full == original
    assert snapshot["paper_trading"] == account
    assert snapshot["buy_signals"] == signals
    assert snapshot["lifetime"] == {"recommendations": 17}
    assert current["levels"] == original["crypto:SOLUSDT"]["levels"]
    assert current["latest_price"] == 112.0
    assert len(current["buy_gate_history"]) == 5
    assert current["buy_gate_history"] == original["crypto:SOLUSDT"]["buy_gate_history"][-5:]
    assert snapshot["full_runner_intelligence"] == build_runner_intelligence_report(
        list(full.values()), signals)
    assert snapshot["full_runner_intelligence"]["buy_gate_failure_tracker"]["event_count"] == 40
    assert current["grade_history"] == original["crypto:SOLUSDT"]["grade_history"]
    assert len(current["signal_history"]) == 3
    assert current["signal_history"][-1]["scan_id"] == "167"
    assert len(current["paired_entry_history"]) == 4
    assert len(current["price_history"]) == 2
    assert len(archived["signal_history"]) == 1
    assert archived["price_history"] == []
    assert archived["paired_entry_history"] == []
    assert snapshot["render_compact_snapshot"] is True
    assert len(json.dumps(snapshot)) < len(json.dumps(full)) / 3


def test_sender_writes_compact_github_fallback_and_posts_exact_same_snapshot(tmp_path, monkeypatch):
    saved = tmp_path / "render_dashboard_snapshot.json"
    monkeypatch.setattr(send_alerts, "RENDER_SNAPSHOT_STATE", saved)
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "test-only-token")
    received = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"status":"synced","records":2}'

    def fake_urlopen(request, timeout):
        payload = request.data
        if dict((k.lower(), v) for k, v in request.header_items()).get("content-encoding") == "gzip":
            payload = gzip.decompress(payload)
        received.append(json.loads(payload))
        return Response()

    monkeypatch.setattr(send_alerts, "urlopen", fake_urlopen)
    original = _tracker()
    source = copy.deepcopy(original)
    account = _account()
    send_alerts.sync_history_to_site(source, {"recommendations": 2},
                                     datetime.now(timezone.utc),
                                     paper_account=account)
    saved_snapshot = json.loads(saved.read_text(encoding="utf-8"))
    assert received == [saved_snapshot]
    assert saved_snapshot["paper_trading"] == account
    assert saved_snapshot["full_runner_intelligence"]["buy_gate_failure_tracker"]["event_count"] == 40
    assert len(saved_snapshot["paper_tracker"]["crypto:SOLUSDT"]["signal_history"]) == 3
    assert source == original


def test_github_fallback_fetches_only_compact_snapshot(monkeypatch, tmp_path):
    snapshot = build_render_snapshot(_tracker(), {"recommendations": 2}, datetime.now(timezone.utc))
    raw = json.dumps(snapshot).encode("utf-8")
    calls = []

    class ApiResponse:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    def get(url, **kwargs):
        calls.append(url)
        assert url.endswith("/contents/render_dashboard_snapshot.json")
        return ApiResponse({"encoding": "base64",
                            "content": base64.b64encode(raw).decode("ascii")})

    monkeypatch.setattr(main.HTTP, "get", get)
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", tmp_path / "nonexistent.json")
    monkeypatch.setattr(main, "HISTORY_LOCAL_COMPACT", tmp_path / "nonexistent-local.json")
    with main.GITHUB_HISTORY_CACHE_LOCK:
        main.GITHUB_HISTORY_CACHE["at"] = 0.0
        main.GITHUB_HISTORY_CACHE["data"] = None
    fetched = main.latest_history_state()
    assert fetched["render_compact_snapshot"] is True
    assert fetched["history_source"] == "github_compact_snapshot"
    assert len(calls) == 1
    assert not any("paper_tracker.json" in url for url in calls)


def test_no_full_ledger_fallback_when_compact_snapshot_missing(monkeypatch):
    monkeypatch.setattr(main, "latest_history_state", lambda: {})
    monkeypatch.setattr(main, "PAPER_STATE", None)
    result = main.history_data()
    assert result["paper_tracker"] == {}
    assert result["history_source"] == "compact_snapshot_pending"
