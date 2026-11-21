"""Webull approved-token persistence never writes credentials to tracked files."""
import base64
import os
from pathlib import Path

import pytest
from webull_token_store import verified_token_file_bytes, verified_token_directory


def encoded(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


TOKEN = "validSecretTokenForReadOnlyWebullSDK.1234"
NORMAL = encoded(TOKEN + "\n1795229999999\nNORMAL\n")


def test_restore_normal_token_in_private_temporary_directory():
    with verified_token_directory(NORMAL) as directory:
        path = Path(directory) / "token.txt"
        assert path.read_text() == TOKEN + "\n1795229999999\nNORMAL\n"
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
            assert Path(directory).stat().st_mode & 0o077 == 0
    assert not Path(directory).exists()


@pytest.mark.parametrize("data", [
    "", "!!!", encoded("not-an-sdk-token"), encoded(TOKEN + "\n100\nPENDING\n"),
    encoded(TOKEN + "\n100\nEXPIRED\n"), encoded(TOKEN + "\nnot-a-date\nNORMAL\n"),
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
