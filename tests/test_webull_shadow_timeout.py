"""Webull shadow requests must not block the scan or expose API credentials."""
import json
import subprocess
from types import SimpleNamespace

import pytest

import main


def test_webull_quote_is_isolated_and_credentials_stay_out_of_arguments(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key-private")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret-private")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "quote": {"price": 123.4, "as_of": "2026-11-20T20:00:00+00:00", "age_minutes": 1}
        }))

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    quote = main.webull_latest_bar("AAPL", "M15", 45)
    assert quote["price"] == 123.4
    args, kwargs = seen[0]
    assert args[1:3] == ["-m", "webull_quote_worker"]
    assert "test-key-private" not in " ".join(args)
    assert "test-secret-private" not in " ".join(args)
    assert kwargs["timeout"] == main.WEBULL_QUOTE_TIMEOUT_SECONDS
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False


def test_webull_pending_verification_is_hard_timed_out(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")

    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(main.subprocess, "run", hangs)
    with pytest.raises(TimeoutError, match="exceeded"):
        main.webull_latest_bar("AAPL", "M15", 45)


def test_webull_permission_failure_is_visible_without_using_response_body(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps({
            "error": "Webull OpenAPI market-data permission denied (HTTP 403)"
        })
    ))
    with pytest.raises(PermissionError, match="HTTP 403"):
        main.webull_latest_bar("AAPL", "M15", 45)


def test_webull_missing_credentials_stops_before_launch(monkeypatch):
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: pytest.fail("worker should not start"))
    with pytest.raises(RuntimeError, match="not configured"):
        main.webull_latest_bar("AAPL")
