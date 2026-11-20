# Jarvis broad historical market research (separate from live scanner)

## Purpose and status (November 19, 2026)

Build a **broad, provenance-preserving, read-only research dataset** with price
history for assets beyond Jarvis's current 146 stock / 92 crypto scan candidates.
Historical OHLCV cannot be substituted for live, verified BUY prices or for
realistic executed-order fills. Nothing here changes the 15-minute scan cadence,
BUY/risk gates, paper-account capital, provider budgets, or notification logic.

### Actual public source inventories captured

- `stocks_universe_2026-11.txt`: **13,814** unique stock/ETF ticker
  identifiers from [TickerAtlas](https://github.com/MeridianAlgo/TickerAtlas)
  (includes global names; **not** 13,814 verified currently tradable US stocks).
- `crypto_universe_2026-11.txt`: **12,242** crypto **base-symbol**
  identifiers from the same atlas. Not all have valid spot markets. BTC is a
  base symbol; BTCUSDT and BTCUSDC are separate price series.
- `stock_daily_archive_manifest_2026-11.csv`: **518** per-stock Parquet
  price-series references, updated through **2026-11-18**, from
  [Auto-Index-Constituents-Tracker](https://github.com/floyds1995/Auto-Index-Constituents-Tracker).
  It covers current S&P 500 and Nasdaq-100 constituents with daily OHLCV as
  early as 2019 (individual histories vary). It has **survivorship bias** and
  is not representative of a full small-cap runner universe.

The three files above are *reference catalog snapshots*. They are not claims
that 518 binary Parquet archives, all 13,814 stock candle histories, or
12,242 crypto market histories have been downloaded into Jarvis.

### Broad price sources

1. **Stock (ready-made GitHub archives):** 518 historical US stock/ETF daily
   Parquet series in the above source. The loader can download them directly
   with source/sha256 manifests; source data originates with Yahoo Finance
   and is subject to upstream rights despite the source repo's MIT code license.
2. **Stock (additional breadth; later stage):** the existing
   [Alpaca Basic](https://docs.alpaca.markets/us/docs/about-market-data-api)
   credentials support **IEX-only** historical stock bars (200 API calls/min,
   free Basic tier; latest 15 minutes limited). These data are venue-limited,
   especially for smaller stocks. Jarvis already pulls Yahoo daily history;
   copying the same daily series is not genuinely new signal evidence.
3. **Crypto (deep market-wide candles):**
   [Binance Vision public archive](https://github.com/binance/binance-public-data)
   publishes historical spot OHLCV for exchange trading **pairs** with
   1h/15m/1d and more intervals. `backfill.py` discovers actually trading
   Binance spot pairs and downloads two **completed** months of hourly
   history (15m is optional). When run November 19, the completed months are
   **September and October 2026**; **November 1–19 requires separate daily
   archives** and is not captured by the monthly collector.
4. **Additional small GitHub dataset:** [Speirsy11/crypto-dataset](https://github.com/Speirsy11/crypto-dataset)
   provides 1-minute-derived Parquet for ten major pairs under an explicit
   data dedication and is useful for independently comparing the parser;
   this is **not** a full-universe source.

### Running the research-only collector

From the private Jarvis repo on a computer with internet and disk:

```bash
python research_data_catalog/backfill.py --asset stock --output .local_history_research --max-files 518 --max-mib 100
python research_data_catalog/backfill.py --asset crypto --output .local_history_research --max-files 1000 --max-mib 200
# Next shard (does not redo existing downloaded files):
python research_data_catalog/backfill.py --asset crypto --output .local_history_research --offset 1000 --max-files 1000 --max-mib 200
# Intraday backtesting research, all spot pairs at 15-minute interval:
python research_data_catalog/backfill.py --asset crypto --interval 15m --output .local_history_research --max-files 1000 --max-mib 300
```

The collector validates Parquet/ZIP signatures and sampled crypto candle
structure, records checksums, and resumes previously acquired files. Each
invocation is bounded by `--max-files`, `--max-mib`, and `--max-runtime`
(default 240 seconds). Invalid or unavailable historical pairs are reported
as missing, never silently filled. The research directory must remain outside
Git history; do **not** add downloaded archives to the scanner's Git state.

A dry run inspects the source catalog/exchange listing but does not download
price archives: `python research_data_catalog/backfill.py --asset stock --dry-run`.
Using a second computer/run requires copying the archived dataset and its
manifest; run-local manifests alone do not prove the archive is complete.

### Optional GitHub Actions collection (manual launch only)

The GitHub Actions workflow `.github/workflows/jarvis-historical-research-archive.yml`
is **workflow_dispatch only** — it does **not** start automatically, is not
part of the 15-minute market scanner, and never trades. From the repository's
Actions tab, select **Jarvis Historical Research Archive** and choose
`stock` or `crypto`, `crypto_interval`, and optional `offset`.

Each manual run caps collection at **550 files, 85 MiB, or 300 seconds**,
whichever comes first, then uploads a short-lived **3-day GitHub artifact**.
Its downloaded ZIP/Parquet prices live in that **artifact**, not the repo.
A later shard uses another offset; because separate runners have separate
disks, download and consolidate those artifact archives on a persistent
storage device for full coverage. **Action minutes and artifact storage count
against the user's GitHub allowance**; use only if the user has budget.
This manual workflow was added but **not dispatched** in this change.

### Critical constraints before retrospective signal testing

- **No training on future data**: reconstruct per-timeframe indicators using
  bars that were available at the simulated decision time. No future news,
  volume, target levels, or post-split membership leakage.
- **Instrument identity**: historical delistings, ticker renames, reverse
  splits, corporate actions, quote currency, and different exchange venues
  must remain distinguishable.
- **Data integrity**: timestamps (Binance spot uses microseconds for 2025+),
  OHLC/volume validity, missing bar count, duplicates, calendar session,
  and independent cross-checks must be measured before analysis.
- **Backtest vs. paper performance**: old bars permit hypothetical studies
  of signal logic; they do not recreate real-time discovery latency, live
  spreads, actual fills, or the performance of the frozen 15-minute strategy.
- **Licensing**: Binance Vision's August 2026 terms license archive data
  under **CC BY-NC-SA 4.0** plus further conditions, permitting purely
  personal **non-production historical backtesting**, not paid signals or
  live execution. Keep raw Binance history in local/private research only.
  Do not resell or publish raw third-party datasets.
- **Costs/storage**: no automatic GitHub Actions job or Render API polling has
  been added to consume extra minutes, storage or paid quotas. A mass backfill
  requires a durable external archive destination; do not put gigabytes into
  the Git repository or transient Render disk.
