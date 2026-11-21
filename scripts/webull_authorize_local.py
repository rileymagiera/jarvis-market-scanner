"""One-time, LOCAL bootstrap of a Webull read-only SDK token.

Run on your own trusted computer with GitHub CLI already authenticated.
This script never displays the token or commits it to the public repo.
"""
import base64
import getpass
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from webull_token_store import verified_token_file_bytes

REPO = "rileymagiera/jarvis-market-scanner"
SECRET = "WEBULL_OPENAPI_TOKEN_FILE_B64"


def save_approved_token_with_gh(token_file: Path) -> None:
    encoded = base64.b64encode(token_file.read_bytes()).decode("ascii")
    verified_token_file_bytes(encoded)
    try:
        result = subprocess.run(
            ["gh", "secret", "set", SECRET, "--repo", REPO],
            input=encoded + "\n", text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run GitHub CLI to save token; no token was printed") from exc
    if result.returncode != 0:
        raise RuntimeError("GitHub CLI did not confirm the secret update; check gh auth and permissions")


def main() -> int:
    print("Webull token setup (read-only): this program NEVER places orders.")
    print("Use only your own trusted device. Do not paste any credentials into chat.")
    app_key = getpass.getpass("Webull OpenAPI app key: ").strip()
    app_secret = getpass.getpass("Webull OpenAPI app secret: ").strip()
    if not app_key or not app_secret:
        print("Webull app credentials are required.")
        return 1
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-webull-authorize-") as tmpdir:
            Path(tmpdir).chmod(0o700)
            # Webull's DataClient creates webull_data_sdk.log in the current
            # directory. Keep that log and the token inside the private temp
            # directory, never in the public repository checkout / Codespace.
            original_cwd = Path.cwd()
            try:
                os.chdir(tmpdir)
                os.environ["WEBULL_OPENAPI_TOKEN_DIR"] = tmpdir
                from webull.core.client import ApiClient
                from webull.data.data_client import DataClient

                print("A Webull app approval may appear. Approve only this expected OpenAPI request.")
                api = ApiClient(app_key, app_secret, "us", token_check_duration_seconds=300)
                api.add_endpoint("us", "api.webull.com")
                # Client initialization itself verifies and persists the 2FA
                # token. No quote, history, account or trading endpoint needed.
                try:
                    DataClient(api)
                except Exception:
                    pass
                path = Path(tmpdir) / "token.txt"
                if not path.is_file():
                    print("No approved token file was produced. Check Webull's OpenAPI authorization.")
                    return 1
                try:
                    save_approved_token_with_gh(path)
                except (RuntimeError, ValueError) as exc:
                    # SDK and credential contents are never included.
                    print(type(exc).__name__ + ": " + str(exc))
                    return 1
            finally:
                os.chdir(original_cwd)
    except Exception as exc:
        print("Webull authorization did not complete (" + type(exc).__name__ + ").")
        return 1
    finally:
        os.environ.pop("WEBULL_OPENAPI_TOKEN_DIR", None)
    print("Verified Webull token securely saved as an encrypted GitHub Actions secret.")
    print("Automatic Webull sampling remains paused until you explicitly enable it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
