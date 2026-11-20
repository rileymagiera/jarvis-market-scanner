"""History synchronization must preserve every field and work with old JSON clients."""
import asyncio
import gzip
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import main
import send_alerts


class RequestStub:
    def __init__(self, data, encoding=""):
        self._data = data
        self.headers = {"content-encoding": encoding}

    async def body(self):
        return self._data


def _payload():
    return {
        "paper_tracker": {
            "crypto:SOLUSDT": {
                "asset_type": "crypto",
                "symbol": "SOLUSDT",
                "price_validation_status": "unverified",
                "discovery_initial_price": None,
                "frozen_research": {"original_signal": [1, 2, {"note": "never truncate history"}]},
                "snapshots": ["complete"] * 100,
            }
        },
        "lifetime": {"recommendations": 17},
        "buy_signals": [{"symbol": "SOLUSDT", "stage": "WATCH"}],
        "paper_trading": {"balance": 250, "positions": []},
        "scan_diagnostics": {"provider": "kraken"},
        "scan_run_id": "123",
    }


def test_plain_and_gzip_history_sync_store_identical_data(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "sample-token")
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", tmp_path / "snapshot.json")
    expected = _payload()
    raw = json.dumps(expected).encode("utf-8")

    plain_result = asyncio.run(main.history_sync_endpoint(
        RequestStub(raw), x_jarvis_history_token="sample-token"))
    plain_state = json.loads(main.HISTORY_SNAPSHOT.read_text(encoding="utf-8"))

    gzip_result = asyncio.run(main.history_sync_endpoint(
        RequestStub(gzip.compress(raw), "gzip"), x_jarvis_history_token="sample-token"))
    gzip_state = json.loads(main.HISTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert gzip_result == plain_result == {"status": "synced", "records": 1}
    assert gzip_state == plain_state
    assert gzip_state["paper_tracker"]["crypto:SOLUSDT"]["frozen_research"] == expected["paper_tracker"]["crypto:SOLUSDT"]["frozen_research"]
    assert gzip_state["paper_trading"] == expected["paper_trading"]


def test_history_sync_rejects_unauthorized_before_parsing_gzip(monkeypatch):
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "secret")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.history_sync_endpoint(
            RequestStub(b"invalid gzip", "gzip"), x_jarvis_history_token="wrong"))
    assert exc.value.status_code == 401


def test_history_sync_rejects_invalid_or_oversized_gzip(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        main.decode_history_sync_payload(b"not gzip", "gzip")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        main.decode_history_sync_payload(b"{}", "brotli")
    assert exc.value.status_code == 415

    monkeypatch.setattr(main, "MAX_HISTORY_SYNC_BYTES", 100)
    with pytest.raises(HTTPException) as exc:
        main.decode_history_sync_payload(gzip.compress(b"x" * 101), "gzip")
    assert exc.value.status_code == 413


class ResponseStub:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b'{"status":"synced","records":1}'


def test_sender_compresses_large_payload_losslessly(monkeypatch):
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "test-token")
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_GZIP_ENABLED", "true")
    requests = []

    def mock_urlopen(req, timeout):
        requests.append(req)
        return ResponseStub()

    monkeypatch.setattr(send_alerts, "urlopen", mock_urlopen)
    tracker = {"crypto:SOLUSDT": {"snapshots": ["repeat-data-"] * 40000}}
    send_alerts.sync_history_to_site(tracker, {"recommendations": 1}, datetime.now(timezone.utc))

    assert len(requests) == 1
    req = requests[0]
    headers = dict((k.lower(), v) for k, v in req.header_items())
    assert headers["content-encoding"] == "gzip"
    payload = json.loads(gzip.decompress(req.data))
    assert payload["paper_tracker"] == tracker
    assert payload["lifetime"] == {"recommendations": 1}
    assert len(req.data) < len(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("legacy_status", [400, 415, 422])
def test_sender_falls_back_to_original_json_for_legacy_receiver(monkeypatch, legacy_status):
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "test-token")
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_GZIP_ENABLED", "true")
    seen = []

    def mock_urlopen(req, timeout):
        seen.append(req)
        headers = dict((k.lower(), v) for k, v in req.header_items())
        if headers.get("content-encoding") == "gzip":
            raise HTTPError(req.full_url, legacy_status, "Legacy receiver rejected gzip", {}, None)
        return ResponseStub()

    monkeypatch.setattr(send_alerts, "urlopen", mock_urlopen)
    tracker = {"crypto:SOLUSDT": {"snapshots": ["repeat-data-"] * 40000}}
    send_alerts.sync_history_to_site(tracker, {}, datetime.now(timezone.utc))

    assert len(seen) == 2
    assert "content-encoding" not in dict((k.lower(), v) for k, v in seen[1].header_items())
    assert json.loads(seen[1].data)["paper_tracker"] == tracker


def test_sync_prefers_scanner_baseline_over_conflicting_render_snapshot(tmp_path, monkeypatch):
    """The display must not jump from +28% to -5% on a nearly flat quote."""
    monkeypatch.setenv("JARVIS_HISTORY_SYNC_TOKEN", "sample-token")
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", tmp_path / "history_snapshot.json")
    timestamp = "2026-11-19T18:45:00+00:00"
    previous = {
        "paper_tracker": {
            "stock:PYXS": {
                "discovery_initial_price": 1.9747,
                "discovery_price_as_of": timestamp,
                "discovery_baseline_status": "first_reliable_fallback",
                "discovery_return_pct": -3.53,
            }
        },
        "lifetime": {},
    }
    main.HISTORY_SNAPSHOT.write_text(json.dumps(previous), encoding="utf-8")
    incoming = {
        "paper_tracker": {
            "stock:PYXS": {
                "asset_type": "stock",
                "symbol": "PYXS",
                "first_seen": timestamp,
                "discovery_initial_price": 1.485,
                "discovery_price_as_of": timestamp,
                "discovery_baseline_status": "unverified_at_discovery",
                "discovery_return_status": "provisional",
                "latest_price": 1.875,
                "price_validation_status": "verified",
            },
            "stock:OTHER": {"symbol": "OTHER", "unrelated_field": [1, 2, 3]},
        },
        "lifetime": {"recommendations": 2},
    }
    result = main.history_sync(incoming, x_jarvis_history_token="sample-token")
    saved = json.loads(main.HISTORY_SNAPSHOT.read_text(encoding="utf-8"))
    pyxs = saved["paper_tracker"]["stock:PYXS"]
    assert result == {"status": "synced", "records": 2}
    assert pyxs["discovery_initial_price"] == 1.485
    assert pyxs["discovery_baseline_status"] == "unverified_at_discovery"
    assert pyxs["discovery_return_pct"] == round((1.875 / 1.485 - 1) * 100, 6)
    assert pyxs["discovery_return_status"] == "provisional"
    assert pyxs["discovery_baseline_conflict"]["render_snapshot_price"] == 1.9747
    assert saved["paper_tracker"]["stock:OTHER"]["unrelated_field"] == [1, 2, 3]

    # Subsequent scans use the same ledger baseline, regardless of Render history.
    incoming["paper_tracker"]["stock:PYXS"]["latest_price"] = 1.905
    main.history_sync(incoming, x_jarvis_history_token="sample-token")
    saved = json.loads(main.HISTORY_SNAPSHOT.read_text(encoding="utf-8"))
    pyxs = saved["paper_tracker"]["stock:PYXS"]
    assert pyxs["discovery_initial_price"] == 1.485
    assert pyxs["discovery_return_pct"] == round((1.905 / 1.485 - 1) * 100, 6)
    assert "discovery_baseline_conflict" not in pyxs


def test_sync_only_recovers_missing_baseline_without_mixing_histories(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", tmp_path / "history_snapshot.json")
    main.HISTORY_SNAPSHOT.write_text(json.dumps({
        "paper_tracker": {
            "stock:PYXS": {
                "discovery_initial_price": 1.485,
                "discovery_price_as_of": "2026-11-19T18:45:00+00:00",
                "discovery_price_source": "original quote",
                "discovery_return_pct": 10,
            },
            "stock:DIFFERENT": {
                "discovery_initial_price": 10,
                "discovery_max_gain_pct": 80,
            },
        },
        "lifetime": {},
    }), encoding="utf-8")
    payload = {"paper_tracker": {
        "stock:PYXS": {
            "discovery_initial_price": None,
            "latest_price": 1.875,
            "price_validation_status": "verified",
        },
        "stock:DIFFERENT": {
            "discovery_initial_price": 12,
            "latest_price": 13,
            "price_validation_status": "unverified",
        },
    }}
    main.apply_discovery_baseline_guard(payload)
    recovered = payload["paper_tracker"]["stock:PYXS"]
    assert recovered["discovery_initial_price"] == 1.485
    assert recovered["discovery_price_source"] == "original quote"
    assert recovered["discovery_return_pct"] == round((1.875 / 1.485 - 1) * 100, 6)
    disagree = payload["paper_tracker"]["stock:DIFFERENT"]
    assert disagree["discovery_initial_price"] == 12
    assert "discovery_max_gain_pct" not in disagree
    assert disagree["discovery_baseline_conflict"]["render_snapshot_price"] == 10
