"""Render should reuse its decoded replica without duplicating memory on each view."""
import json
import os

os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import main


class CountingPath:
    def __init__(self, path):
        self.path = path
        self.reads = 0

    def __str__(self):
        return str(self.path)

    def stat(self):
        return self.path.stat()

    def read_text(self, **kwargs):
        self.reads += 1
        return self.path.read_text(**kwargs)

    def write_text(self, *args, **kwargs):
        return self.path.write_text(*args, **kwargs)


def _write_snapshot(path, scan):
    path.write_text(json.dumps({
        "paper_tracker": {"stock:ABC": {"asset_type": "stock", "symbol": "ABC",
                                          "currently_surfaced": True}},
        "lifetime": {"recommendations": 1},
        "scan_run_id": scan,
        "paper_trading": {"version": 1, "cash": 250, "positions": []},
        "render_compact_snapshot": True,
    }), encoding="utf-8")


def test_dashboard_requests_reuse_snapshot_and_reload_changed_file(tmp_path, monkeypatch):
    path = tmp_path / "history_snapshot.json"
    _write_snapshot(path, "first-scan")
    counted = CountingPath(path)
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", counted)
    monkeypatch.setattr(main, "github_history_state", lambda: {})
    first = main.latest_history_state()
    second = main.latest_history_state()
    assert first["scan_run_id"] == second["scan_run_id"] == "first-scan"
    assert first["paper_trading"] == second["paper_trading"]
    assert counted.reads == 1

    _write_snapshot(path, "second-longer-scan-id")
    new_state = main.latest_history_state()
    assert new_state["scan_run_id"] == "second-longer-scan-id"
    assert counted.reads == 2


def test_history_sync_invalidates_cached_snapshot_and_preserves_account(tmp_path, monkeypatch):
    path = tmp_path / "history_snapshot.json"
    _write_snapshot(path, "prior")
    counted = CountingPath(path)
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", counted)
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "test-secret")
    assert main.latest_history_state()["scan_run_id"] == "prior"
    assert counted.reads == 1

    original_account = {"version": 1, "cash": 123, "positions": [
        {"symbol": "BTCUSDT", "qty": 0.01, "target_1": 100000}]}
    payload = {
        "paper_tracker": {"crypto:BTCUSDT": {"symbol": "BTCUSDT", "asset_type": "crypto"}},
        "lifetime": {"recommendations": 2},
        "scan_run_id": "new-scan",
        "paper_trading": original_account,
        "render_compact_snapshot": True,
    }
    result = main.history_sync(payload, x_jarvis_history_token="test-secret")
    assert result == {"status": "synced", "records": 1}
    refreshed = main.latest_history_state()
    assert refreshed["scan_run_id"] == "new-scan"
    assert refreshed["paper_trading"] == original_account
    assert counted.reads == 2
