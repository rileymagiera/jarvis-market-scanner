"""Secure restoration of an approved Webull OpenAPI SDK token on ephemeral runners.

The SDK stores three lines in token.txt: token, expiration, NORMAL.
Only an encrypted GitHub Actions repository secret should carry this base64
representation between runs. Never use tracked files or Actions cache.
"""
from contextlib import contextmanager
import base64
import binascii
import os
from pathlib import Path
import re
import tempfile


TOKEN_SECRET_ENV = "WEBULL_OPENAPI_TOKEN_FILE_B64"
TOKEN_DIR_ENV = "WEBULL_OPENAPI_TOKEN_DIR"
MAX_TOKEN_FILE_BYTES = 4096


def verified_token_file_bytes(encoded: str) -> bytes:
    """Validate the exact three-line SDK format without exposing token values."""
    if not encoded or not isinstance(encoded, str):
        raise ValueError("Approved Webull token secret is not configured")
    try:
        data = base64.b64decode(encoded.strip(), validate=True)
        value = data.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Approved Webull token secret is not valid base64/UTF-8") from exc
    if not data or len(data) > MAX_TOKEN_FILE_BYTES:
        raise ValueError("Approved Webull token file has invalid length")
    lines = value.splitlines()
    if len(lines) != 3:
        raise ValueError("Approved Webull token file must have exactly three lines")
    token, expires, status = lines
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]{8,2048}", token):
        raise ValueError("Approved Webull token has invalid format")
    if not expires.isascii() or not expires.isdecimal() or int(expires) <= 0:
        raise ValueError("Approved Webull token expiry has invalid format")
    if status != "NORMAL":
        raise ValueError("Webull token is not verified as NORMAL")
    return (token + "\n" + expires + "\n" + status + "\n").encode("utf-8")


@contextmanager
def verified_token_directory(encoded: str):
    """Provide a private directory for SDK token reuse, cleaned after the call."""
    data = verified_token_file_bytes(encoded)
    with tempfile.TemporaryDirectory(prefix="jarvis-webull-token-") as tmp_dir:
        directory = Path(tmp_dir)
        directory.chmod(0o700)
        token_path = directory / "token.txt"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(token_path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except BaseException:
            token_path.unlink(missing_ok=True)
            raise
        yield str(directory)
