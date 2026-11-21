"""Webull approved-token persistence never writes credentials to tracked files."""
import base64
import os
from pathlib import Path

import pytest
from webull_token_store import verified_token_file_bytes, verified_token_directory


def encoded(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


TOKEN = "validSecretTokenForReadOnlyWebullSDK.1234"
NORMAL = encoded(TOKEN + "\n4102444800000\nNORMAL\n")


def test_restore_normal_token_in_private_temporary_directory():
    with verified_token_directory(NORMAL) as directory:
        path = Path(directory) / "token.txt"
        assert path.read_text() == TOKEN + "\n4102444800000\nNORMAL\n"
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
            assert Path(directory).stat().st_mode & 0o077 == 0
    assert not Path(directory).exists()


@pytest.mark.parametrize("data", [
    "", "!!!", encoded("not-an-sdk-token"), encoded(TOKEN + "\n100\nPENDING\n"),
    encoded(TOKEN + "\n100\nEXPIRED\n"), encoded(TOKEN + "\nnot-a-date\nNORMAL\n"),
    encoded(TOKEN + "\n1600000000000\nNORMAL\n"), encoded(TOKEN + "\n1\nNORMAL\n"),
    encoded("short\n100\nNORMAL\n"), encoded(TOKEN + "\n100\nNORMAL\nINJECTED\n"),
])
def test_invalid_or_unapproved_tokens_fail_closed(data):
    with pytest.raises(ValueError) as error:
        verified_token_file_bytes(data)
    assert TOKEN not in str(error.value)


def test_no_unapproved_token_directory_created(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    with pytest.raises(ValueError):
        with verified_token_directory(encoded(TOKEN + "\n100\nPENDING\n")):
            pass
    assert list(tmp_path.iterdir()) == []


def test_worker_does_not_contact_webull_without_secret(monkeypatch, capsys):
    import sys
    import webull_quote_worker
    monkeypatch.delenv("WEBULL_OPENAPI_TOKEN_FILE_B64", raising=False)
    monkeypatch.setattr(sys, "argv", ["worker", "AAPL", "M15", "45"])
    assert webull_quote_worker.main() == 1
    output = capsys.readouterr().out
    assert "approved token" in output.lower()


def test_worker_uses_private_temp_token_without_exposing_it(monkeypatch, capsys):
    import json
    import sys
    import main
    import webull_quote_worker

    seen = []

    def fake_direct(symbol, interval, age):
        token_dir = Path(os.environ["WEBULL_OPENAPI_TOKEN_DIR"])
        secret_file = token_dir / "token.txt"
        assert secret_file.read_text() == TOKEN + "\n4102444800000\nNORMAL\n"
        seen.append(str(token_dir))
        return {"price": 200.0, "as_of": "2026-11-20T20:00:00Z", "age_minutes": 0}

    monkeypatch.setenv("WEBULL_OPENAPI_TOKEN_FILE_B64", NORMAL)
    monkeypatch.setattr(main, "_webull_latest_bar_direct", fake_direct)
    monkeypatch.setattr(sys, "argv", ["worker", "AAPL", "M15", "45"])
    assert webull_quote_worker.main() == 0
    assert json.loads(capsys.readouterr().out)["quote"]["price"] == 200
    assert len(seen) == 1
    assert not Path(seen[0]).exists()


def test_local_bootstrap_sends_secret_using_stdin_only(tmp_path, monkeypatch):
    from scripts.webull_authorize_local import save_approved_token_with_gh
    from types import SimpleNamespace
    import subprocess
    token_file = tmp_path / "token.txt"
    token_file.write_text(TOKEN + "\n4102444800000\nNORMAL\n")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    save_approved_token_with_gh(token_file)
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args[:3] == ["gh", "secret", "set"]
    assert TOKEN not in " ".join(args)
    assert kwargs["input"].strip() == NORMAL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE
