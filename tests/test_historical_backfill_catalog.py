"""Safe backfill planning and archive-format checks; no external HTTP calls."""
import io
import zipfile
from datetime import datetime, timezone

from research_data_catalog.backfill import (
    recent_completed_months, stock_plan, crypto_plan, valid_parquet,
    valid_kline_zip, download,
)


def sample_zip(rows):
    handle = io.BytesIO()
    with zipfile.ZipFile(handle, "w") as z:
        z.writestr("BTCUSDT-1h-2026-10.csv", "\n".join(",".join(map(str, row)) for row in rows))
    return handle.getvalue()


def test_stock_archive_manifest_is_sorted_and_point_in_time_explicit():
    raw = "ticker,rows,first_date,last_date,file_size_kb\nB,2,2019-01-01,2026-11-18,4\nA,5,2019-01-01,2026-11-18,4\n"
    plan = stock_plan(raw)
    assert [x["symbol"] for x in plan] == ["A", "B"]
    assert all(x["asset"] == "stock" and x["interval"] == "1d" for x in plan)
    assert all("parquet" in x["url"] for x in plan)


def test_crypto_months_exclude_unfinished_current_month():
    assert recent_completed_months(datetime(2026, 11, 19, tzinfo=timezone.utc)) == ["2026-09", "2026-10"]
    assert recent_completed_months(datetime(2027, 1, 2, tzinfo=timezone.utc)) == ["2026-11", "2026-12"]


def test_crypto_plan_handles_all_spot_pairs_or_quote_filter():
    payload = {"symbols": [
        {"symbol": "SOLUSDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
        {"symbol": "SOLBTC", "status": "TRADING", "quoteAsset": "BTC", "isSpotTradingAllowed": True},
        {"symbol": "FAKEUSDT", "status": "BREAK", "quoteAsset": "USDT"},
        {"symbol": "../BAD", "status": "TRADING", "quoteAsset": "USDT"},
    ]}
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    all_rows = crypto_plan(payload, now)
    assert len(all_rows) == 4
    assert len(crypto_plan(payload, now, quote_filter=["USDT"])) == 2
    assert all("2026-10.zip" in x["url"] or "2026-09.zip" in x["url"] for x in all_rows)
    assert all(x["interval"] == "1h" for x in all_rows)


def test_archive_validation_rejects_non_parquet_and_invalid_zip():
    assert valid_parquet(b"PAR1hello-worldPAR1")
    assert not valid_parquet(b"<html>Blocked</html>")
    one = [1790812800000000, 2.0, 3.0, 1.5, 2.5, 12.0, 0, 0, 0, 0, 0, 0]
    two = [1790816400000000, 2.5, 4.0, 2.0, 3.0, 12.0, 0, 0, 0, 0, 0, 0]
    assert valid_kline_zip(sample_zip([one, two]))
    assert not valid_kline_zip(sample_zip([two, one]))
    assert not valid_kline_zip(sample_zip([[1790812800000] + one[1:]]))
    assert not valid_kline_zip(sample_zip([[one[0], 4, 2, 1, 3, 12, 0, 0, 0, 0, 0, 0]]))


def test_download_is_separate_from_live_scanner_and_resumable(tmp_path):
    plan = stock_plan("ticker,rows,first_date,last_date,file_size_kb\nA,2,2019-01-01,2026-11-18,3\nB,2,2019-01-01,2026-11-18,3\n")
    calls = []
    def fake_fetch(url):
        calls.append(url)
        return b"PAR1hello-worldPAR1"
    out = tmp_path / "history"
    first = download(plan, out, max_files=1, pause=0, fetch=fake_fetch)
    assert first["downloaded"] == 1
    assert first["next_offset"] == 1
    assert (out / "stocks" / "A.parquet").exists()
    next_run = download(plan, out, max_files=1, pause=0, fetch=fake_fetch)
    assert next_run["already_present"] == 1
    assert next_run["downloaded"] == 1
    assert next_run["next_offset"] == 2
    assert (out / "stocks" / "B.parquet").exists()
    assert not (tmp_path / "paper_trading_state.json").exists()
    assert len(calls) == 2


def test_zero_plan_is_reported_without_invented_candles(tmp_path):
    summary = download([], tmp_path, pause=0)
    assert summary["downloaded"] == 0
    assert summary["source_plan_count"] == 0


def test_next_offset_uses_actual_processed_count_when_byte_cap_interrupts(tmp_path):
    plan = stock_plan("ticker,rows,first_date,last_date,file_size_kb\nA,2,2019-01-01,2026-11-18,3\nB,2,2019-01-01,2026-11-18,3\n")
    def fake_fetch(url):
        return b"PAR1" + b"x" * (700 * 1024) + b"PAR1"
    result = download(plan, tmp_path / "sample", max_files=10, max_mib=1, pause=0, fetch=fake_fetch)
    assert result["downloaded"] == 1
    assert result["next_offset"] == 1


def test_corrupt_existing_stock_archive_is_replaced_not_skipped(tmp_path):
    plan = stock_plan("ticker,rows,first_date,last_date,file_size_kb\\nA,2,2019-01-01,2026-11-18,3\\n")
    # Use a real newline in the synthetic CSV, not a literal escaped sequence.
    plan = stock_plan("ticker,rows\\nA,2\\n".replace("\\\\n", "\\n"))
    output = tmp_path / "archive"
    old = output / "stocks" / "A.parquet"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"partial corrupted archive")
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        return b"PAR1valid-parquet-PAR1"
    result = download(plan, output, max_files=1, fetch=fake_fetch, pause=0)
    assert result["already_present"] == 0
    assert result["downloaded"] == 1
    assert old.read_bytes() == b"PAR1valid-parquet-PAR1"
    assert result["archives_missing_on_disk"] == 0
    assert len(fetched) == 1


def test_independent_shard_reports_are_kept(tmp_path):
    plan = stock_plan("ticker,rows\\nA,2\\nB,2\\n".replace("\\\\n", "\\n"))
    def fake_fetch(url):
        return b"PAR1valid-parquet-PAR1"
    out = tmp_path / "history"
    first = download(plan, out, max_files=1, fetch=fake_fetch, pause=0)
    second = download(plan, out, max_files=1, offset=first["next_offset"], fetch=fake_fetch, pause=0)
    assert first["plan_entries_not_yet_attempted"] == 1
    assert second["plan_entries_not_yet_attempted"] == 0
    assert second["archives_on_disk"] == 2
    assert (out / "stock_1d_offset_0_manifest.json").is_file()
    assert (out / "stock_1d_offset_1_manifest.json").is_file()
    assert (out / "stock_last_run_manifest.json").is_file()
