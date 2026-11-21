"""Compact Render sync must not deserialize the previous full history ledger."""
import json
import os

os.environ.setdefault("JARVIS_GMAIL_USER", "jarvis-test@example.com")
os.environ.setdefault("JARVIS_GMAIL_APP_PASSWORD", "test-only")
os.environ.setdefault("JARVIS_ALERT_TO", "jarvis-test@example.com")

import main


def test_authoritative_compact_sync_skips_previous_huge_snapshot(monkeypatch):
    class ForbiddenRead:
        def read_text(self, **kwargs):
            raise AssertionError("Compact sync must not parse prior full Render snapshot")

    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", ForbiddenRead())
    expected_baseline = 4.25
    payload = {
        "render_compact_snapshot": True,
        "paper_tracker": {
            "stock:RUN": {
                "discovery_initial_price": expected_baseline,
                "latest_price": 5.1,
                "price_validation_status": "verified",
                "price_as_of": "2026-11-20T17:00:00+00:00",
                "data_quality": {"data_type": "delayed"},
            }
        },
    }
    result = main.apply_discovery_baseline_guard(payload)
    row = result["paper_tracker"]["stock:RUN"]
    assert row["discovery_initial_price"] == expected_baseline
    assert row["discovery_return_pct"] == 20.0
    assert row["discovery_return_status"] == "provisional"


def test_legacy_full_sync_still_repairs_missing_baselines(tmp_path, monkeypatch):
    path = tmp_path / "previous.json"
    path.write_text(json.dumps({
        "paper_tracker": {
            "stock:RUN": {
                "discovery_initial_price": 4.25,
                "discovery_price_as_of": "2026-11-19T17:00:00+00:00",
                "discovery_baseline_status": "live_verified",
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(main, "HISTORY_SNAPSHOT", path)
    legacy = {
        "paper_tracker": {
            "stock:RUN": {
                "discovery_initial_price": None,
                "price_validation_status": "unverified",
            }
        }
    }
    row = main.apply_discovery_baseline_guard(legacy)["paper_tracker"]["stock:RUN"]
    assert row["discovery_initial_price"] == 4.25
    assert row["discovery_baseline_status"] == "live_verified"
