"""Bounded one-time, RESEARCH-ONLY market history downloader.

Reads publicly maintained stock archives and Binance Vision monthly spot klines.
NEVER writes Jarvis scan state, alters prices/signals, or places trades.
Run only for personal non-production historical research; check upstream licenses.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import math
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

STOCK_BASE = "https://raw.githubusercontent.com/floyds1995/Auto-Index-Constituents-Tracker/main/us_data/raw_ohlcv"
CRYPTO_ARCHIVE = "https://data.binance.vision/data/spot/monthly/klines"
BINANCE_PAIRS = "https://data-api.binance.vision/api/v3/exchangeInfo"
# Binance exchangeInfo metadata is now larger than 6 MB. Keep a separate
# bounded cap so individual OHLCV download limits remain unchanged.
BINANCE_EXCHANGE_INFO_MAX_BYTES = 40_000_000
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
ROOT = Path(__file__).resolve().parents[1]


def _ticker(value):
    t = str(value).strip().upper()
    if not TICKER_RE.fullmatch(t):
        raise ValueError("Unsafe symbol")
    return t


def _get(url, max_bytes=6_000_000, timeout=20):
    req = Request(url, headers={"User-Agent": "Jarvis-Personal-Research-Historical-Archive/1.0"})
    with urlopen(req, timeout=timeout) as response:
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"Response exceeds {max_bytes:,}-byte configured size bound")
    return content


def recent_completed_months(now, count=2):
    year, month = now.astimezone(timezone.utc).year, now.astimezone(timezone.utc).month
    result = []
    for _ in range(count):
        month -= 1
        if not month:
            year, month = year - 1, 12
        result.append(f"{year:04d}-{month:02d}")
    return sorted(result)


def stock_plan(manifest):
    rows = list(csv.DictReader(io.StringIO(manifest)))
    symbols = sorted({_ticker(row["ticker"]) for row in rows if int(row.get("rows") or 0) > 0})
    return [
        {"asset": "stock", "symbol": s, "interval": "1d", "period": "history",
         "url": f"{STOCK_BASE}/parquet/{s}.parquet",
         "relative": Path("stocks") / f"{s}.parquet"}
        for s in symbols
    ]


def crypto_plan(payload, now, months=2, interval="1h", quote_filter=None):
    if interval not in ("1h", "15m", "1d"):
        raise ValueError("Unsupported interval")
    quotes = {q.upper() for q in (quote_filter or []) if q}
    pairs = set()
    for row in payload.get("symbols") or []:
        try:
            s = _ticker(row["symbol"])
        except (ValueError, KeyError):
            continue
        if row.get("status") != "TRADING" or row.get("isSpotTradingAllowed") is False:
            continue
        if quotes and str(row.get("quoteAsset") or "").upper() not in quotes:
            continue
        pairs.add(s)
    result = []
    for symbol in sorted(pairs):
        for month in recent_completed_months(now, months):
            result.append({
                "asset": "crypto", "symbol": symbol, "interval": interval, "period": month,
                "url": f"{CRYPTO_ARCHIVE}/{symbol}/{interval}/{symbol}-{interval}-{month}.zip",
                "relative": Path("crypto") / interval / symbol / f"{month}.zip",
            })
    return result


def valid_parquet(payload):
    return len(payload) >= 12 and payload.startswith(b"PAR1") and payload.endswith(b"PAR1")


def valid_kline_zip(payload):
    if not payload.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            files = [n for n in z.namelist() if n.endswith(".csv")]
            if len(files) != 1 or z.testzip() is not None:
                return False
            previous = -1
            count = 0
            for row in csv.reader(io.TextIOWrapper(z.open(files[0]), encoding="utf-8")):
                if not row or row[0].lower().replace(" ", "") in ("opentime", "open_time"):
                    continue
                if len(row) < 6:
                    return False
                stamp = int(row[0])
                if stamp < 1_000_000_000_000_000 or stamp <= previous:
                    return False
                previous = stamp
                o, h, l, c, v = map(float, row[1:6])
                if not all(math.isfinite(n) for n in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0 or h < max(o, c) or l > min(o, c):
                    return False
                count += 1
            return count > 0
    except (ValueError, TypeError, UnicodeError, OSError, RuntimeError, zipfile.BadZipFile):
        return False


def download(plan, output, max_files=600, max_mib=100, max_runtime=240, offset=0, fetch=_get, pause=0.08):
    """Bounded, resumable archive collection; never save large files in Git."""
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    downloaded = failed = present = size = 0
    processed = 0
    records = []
    # Per-run offsets allow following shards without changing user credentials.
    for task in plan[offset:]:
        if downloaded >= max_files or size >= max_mib * 1024**2 or time.monotonic() - start > max_runtime:
            break
        path = root / task["relative"]
        if path.is_file():
            try:
                existing = path.read_bytes()
                valid_existing = valid_parquet(existing) if task["asset"] == "stock" else valid_kline_zip(existing)
            except (OSError, ValueError, zipfile.BadZipFile):
                valid_existing = False
            if valid_existing:
                present += 1
                processed += 1
                continue
            # Never silently accept corrupt existing archives. Replace only after validation.
        try:
            data = fetch(task["url"])
            if not (valid_parquet(data) if task["asset"] == "stock" else valid_kline_zip(data)):
                raise ValueError("Unexpected or damaged OHLCV archive")
            if size + len(data) > max_mib * 1024**2:
                break
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".partial")
            tmp.write_bytes(data)
            tmp.replace(path)
            downloaded += 1
            size += len(data)
            records.append({
                "asset": task["asset"], "symbol": task["symbol"],
                "interval": task["interval"], "period": task["period"], "source": task["url"],
                "file": str(task["relative"]), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        except (HTTPError, URLError, ValueError, OSError, TimeoutError, zipfile.BadZipFile) as exc:
            failed += 1
            records.append({"symbol": task["symbol"], "interval": task["interval"],
                            "period": task["period"], "status": "unavailable", "reason": type(exc).__name__})
        processed += 1
        if pause:
            time.sleep(pause)
    on_disk = sum((root / task["relative"]).is_file() for task in plan)
    report = {
        "research_only": True, "source_plan_count": len(plan), "offset": offset,
        "archives_on_disk": on_disk, "archives_missing_on_disk": len(plan) - on_disk,
        "plan_entries_not_yet_attempted": max(0, len(plan) - offset - processed),
        "next_offset": offset + processed,
        "downloaded": downloaded, "already_present": present, "unavailable": failed,
        "downloaded_bytes": size, "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "notice": "No live quotes, verified execution fills or production BUY inputs. Binance Vision data licensed for personal non-production research.",
        "records": records,
    }
    file = root / ("stock" if plan and plan[0]["asset"] == "stock" else "crypto")
    file = file.with_name(file.name + "_last_run_manifest.json")
    encoded = json.dumps(report, indent=2) + "\n"
    file.write_text(encoded, encoding="utf-8")
    # Preserve per-shard evidence when independent Action artifacts are consolidated.
    interval = plan[0]["interval"] if plan else "none"
    shard_file = root / f"{file.stem.removesuffix('_last_run_manifest')}_{interval}_offset_{offset}_manifest.json"
    shard_file.write_text(encoded, encoding="utf-8")
    return report


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--asset", choices=["stock", "crypto"], required=True)
    p.add_argument("--output", default=".local_history_research")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-files", type=int, default=600)
    p.add_argument("--max-mib", type=int, default=100)
    p.add_argument("--max-runtime", type=int, default=240)
    p.add_argument("--months", type=int, default=2)
    p.add_argument("--interval", choices=["1h", "15m", "1d"], default="1h")
    p.add_argument("--quotes", default="", help="Comma-separated crypto quote currencies, empty for all")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if min(args.max_files, args.max_mib, args.max_runtime) <= 0 or args.offset < 0 or not 1 <= args.months <= 3:
        p.error("Invalid bounds")
    if args.asset == "stock":
        manifest = (ROOT / "research_data_catalog/stock_daily_archive_manifest_2026-11.csv").read_text()
        plan = stock_plan(manifest)
    else:
        raw = json.loads(_get(BINANCE_PAIRS, max_bytes=BINANCE_EXCHANGE_INFO_MAX_BYTES, timeout=25))
        plan = crypto_plan(raw, datetime.now(timezone.utc), args.months, args.interval, args.quotes.split(","))
    print(f"Planned {len(plan)} source archives; execution starts at offset {args.offset}.")
    if not args.dry_run:
        summary = download(plan, args.output, args.max_files, args.max_mib, args.max_runtime, args.offset)
        print(json.dumps({key: summary[key] for key in ("downloaded", "already_present", "unavailable", "downloaded_bytes", "next_offset", "archives_on_disk", "archives_missing_on_disk", "plan_entries_not_yet_attempted")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
