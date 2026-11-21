"""Render sync must be smaller without changing GitHub research or paper trading."""
import gzip
import json
import os
from copy import deepcopy
from datetime import datetime, timezone

os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import main
import send_alerts


def sample_tracker():
    histories = [{"scan_id": str(i), "score": i} for i in range(20)]
    gates = [{"run_id": str(i), "passed": i % 2 == 0} for i in range(20)]
    return {
        "stock:RUN": {
            "symbol": "RUN", "asset_type": "stock", "currently_surfaced": True,
            "signal_history": histories, "buy_gate_history": gates,
            "grade_history": [{"at": "2026-11-20", "from": "WATCH", "to": "BUY"}],
            "levels": {"entry_low": 4, "stop_loss": 3.5, "target_1": 5, "target_2": 6},
            "early_runner_initial_feature_snapshot": {"volume": 12345},
            "discovery_initial_price": 4.25,
            "price_validation_status": "verified",
        },
        "crypto:OLD": {
            "symbol": "OLDUSDT", "asset_type": "crypto", "currently_surfaced": False,
            "signal_history": histories, "buy_gate_history": gates,
            "research_outcomes": {"5d": {"return_pct": 8}},
        },
    }


def test_render_projection_keeps_active_recent_evidence_without_mutating_github_data():
    original = sample_tracker()
    preserved = deepcopy(original)
    projected = send_alerts.compact_tracker_for_render(original)
    assert original == preserved
    assert projected is not original
    assert projected["stock:RUN"]["signal_history"] == preserved["stock:RUN"]["signal_history"][-4:]
    assert projected["stock:RUN"]["buy_gate_history"] == preserved["stock:RUN"]["buy_gate_history"][-4:]
    assert projected["crypto:OLD"]["signal_history"] == preserved["crypto:OLD"]["signal_history"][-1:]
    assert projected["crypto:OLD"]["buy_gate_history"] == preserved["crypto:OLD"]["buy_gate_history"][-1:]
    for key, record in original.items():
        for field, value in record.items():
            if field not in ("signal_history", "buy_gate_history"):
                assert projected[key][field] == value
    assert projected["stock:RUN"]["levels"]["stop_loss"] == 3.5
    assert projected["stock:RUN"]["levels"]["target_2"] == 6


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b'{"status":"synced","records":2}'


def test_render_sync_keeps_complete_paper_account_and_trade_configuration(monkeypatch):
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "sample-token")
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_GZIP_ENABLED", "true")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return _Response()

    monkeypatch.setattr(send_alerts, "urlopen", fake_urlopen)
    original = sample_tracker()
    saved = deepcopy(original)
    account = {
        "version": 1, "cash_balance": 181.25,
        "positions": [{"symbol": "RUN", "entry_price": 4, "quantity": 3,
                       "stop_loss": 3.5, "target_1": 5, "target_2": 6}],
        "trades": [{"symbol": "RUN", "side": "BUY", "quantity": 3}],
        "risk_off_comparison": {"summary": {"trades": 7}},
    }
    metadata = {"run_id": "1001", "last_scan_completed_at": "2026-11-21T04:10:00+00:00",
                "scan_diagnostics": {"completed_scan_pair": [{"run_id": "1000"}, {"run_id": "1001"}]}}
    send_alerts.sync_history_to_site(original, {"checkpoints": {"1": {"wins": 2}}},
                                     datetime.now(timezone.utc), [{"symbol": "RUN", "status": "BUY"}],
                                     metadata, account)
    assert len(calls) == 1
    request = calls[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    raw = gzip.decompress(request.data) if headers.get("content-encoding") == "gzip" else request.data
    payload = json.loads(raw)
    assert payload["render_projection_version"] == 1
    assert payload["paper_trading"] == account
    assert payload["scan_diagnostics"] == metadata["scan_diagnostics"]
    assert payload["lifetime"] == {"checkpoints": {"1": {"wins": 2}}}
    assert payload["buy_signals"] == [{"symbol": "RUN", "status": "BUY"}]
    assert payload["paper_tracker"]["stock:RUN"]["signal_history"] == saved["stock:RUN"]["signal_history"][-4:]
    assert original == saved


def test_compact_sync_does_not_load_old_large_render_snapshot(monkeypatch):
    class ForbiddenRead:
        def read_text(self, *args, **kwargs):
            raise AssertionError("Compact sync must not read previous full-size snapshot")

    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", ForbiddenRead())
    record = {
        "discovery_initial_price": 4, "latest_price": 5,
        "price_validation_status": "verified",
        "price_as_of": "2026-11-20T17:00:00+00:00",
        "data_quality": {"data_type": "delayed"},
    }
    payload = {"render_projection_version": 1, "paper_tracker": {"stock:RUN": record}}
    result = main.apply_discovery_baseline_guard(payload)
    assert result["paper_tracker"]["stock:RUN"]["discovery_initial_price"] == 4
    assert result["paper_tracker"]["stock:RUN"]["discovery_return_pct"] == 25
    assert result["paper_tracker"]["stock:RUN"]["discovery_return_status"] == "provisional"
