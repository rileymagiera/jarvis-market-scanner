"""Bounded child-process entry point for read-only Webull market data.

The parent scanner calls this process with a hard wall-clock deadline. Never
pass keys on the command line or write them to files; the child inherits the
GitHub Actions secret environment. No trading clients are instantiated.
"""
import json
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"error": "Invalid Webull quote worker arguments"}))
        return 2
    symbol, interval, max_age = sys.argv[1:]
    try:
        from main import _webull_latest_bar_direct
        quote = _webull_latest_bar_direct(symbol, interval, int(max_age))
        print(json.dumps({"quote": quote}))
        return 0
    except PermissionError:
        print(json.dumps({"error": "Webull OpenAPI market-data permission denied (HTTP 403)"}))
    except Exception as exc:
        # No exception strings or response bodies: SDK errors may contain secrets.
        print(json.dumps({"error": f"Webull quote worker failed ({type(exc).__name__})"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
