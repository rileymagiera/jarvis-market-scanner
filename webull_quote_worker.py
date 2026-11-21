"""Bounded child-process entry point for read-only Webull market data.

The parent scanner calls this process with a hard wall-clock deadline. Never
pass keys on the command line or write them to files; the child inherits the
GitHub Actions secret environment. No trading clients are instantiated.
"""
import json
import os
import sys

from webull_token_store import TOKEN_DIR_ENV, TOKEN_SECRET_ENV, verified_token_directory


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"error": "Invalid Webull quote worker arguments"}))
        return 2
    symbol, interval, max_age = sys.argv[1:]
    try:
        # Fail closed before initializing the SDK, which otherwise creates a
        # fresh PENDING token and triggers another app-approval notification.
        with verified_token_directory(os.getenv(TOKEN_SECRET_ENV, "")) as token_dir:
            os.environ[TOKEN_DIR_ENV] = token_dir
            from main import _webull_latest_bar_direct
            quote = _webull_latest_bar_direct(symbol, interval, int(max_age))
            print(json.dumps({"quote": quote}))
            return 0
    except PermissionError:
        print(json.dumps({"error": "Webull OpenAPI market-data permission denied (HTTP 403)"}))
    except ValueError:
        print(json.dumps({"error": "Webull approved token is absent or invalid; no API request was made"}))
    except Exception as exc:
        # No exception strings or response bodies: SDK errors may contain secrets.
        print(json.dumps({"error": f"Webull quote worker failed ({type(exc).__name__})"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
