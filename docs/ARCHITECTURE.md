# Jarvis architecture and operations

Last reviewed: 2026-11-19

## Runtime topology

```text
cron-job.org
    |
    | workflow_dispatch
    v
GitHub Actions: Jarvis Market Alerts
    |
    +--> run send_alerts.py
    |       |
    |       +--> main.py scan(asset="all")
    |       |       +--> stock and crypto providers
    |       |       +--> technical / regime / setup analysis
    |       |       +--> early-runner and BUY intelligence
    |       |
    |       +--> candidate history + lifecycle updates
    |       +--> isolated $250 paper account
    |       +--> alert selection and email
    |       +--> POST /history/sync
    |
    +--> scan audit + provider usage history + Git commit
    |
    v
Render FastAPI dashboard
    +--> dashboard pages and JSON endpoints
    +--> synchronized history (with committed state as recovery source)
```

The external cron-job.org schedule is the sole scheduler. Do not add a second GitHub cron schedule without deliberately migrating the scheduler and verifying that duplicate scans cannot occur. The workflow uses a concurrency group to serialize scan runs.

## Source-code boundaries

### `main.py` — application and scanner composition

Currently a large combined module (about 3,600 lines at the 2026-11-19 review). Its responsibilities are grouped approximately as follows:

- Lines 1–80: imports, FastAPI setup, authentication, default universes.
- Lines 80–327: symbol discovery and market-session/provider setup.
- Lines 328–842: stock quote/history providers, provider telemetry, bounded enrichment, and market data.
- Lines 843–1,125: market conditions, setup classification, and price levels.
- Lines 1,126–1,519: SEC/news/fundamental enrichment and technical/research metrics.
- Lines 1,520–1,927: stock scan.
- Lines 1,928–2,078: crypto news and provider market data.
- Lines 2,079–2,274: crypto scan, cross-section analysis, parallel scanning, response composition.
- Lines 2,275 onward: FastAPI routes, history/dashboard HTML, paper dashboard, scan endpoints, and Intelligence page. The Home dashboard JavaScript now lives in `static/history_dashboard.js` and is served at `/history/dashboard.js`.

These are approximate line ranges, not stable APIs. Prefer moving one cohesive responsibility at a time and preserving imports/routes until compatibility tests exist.

### Supporting modules

- `early_runner.py`: research feature calculations. Its module docstring explicitly states that these fields do not alter BUY scoring.
- `buy_intelligence.py`: BUY readiness and score blending. Changes here require targeted gate tests and a full regression run.
- `runner_intelligence.py`: research report construction.
- `paper_trading.py`: simulated accounting and paper eligibility. It must never create live orders.
- `send_alerts.py`: scheduled orchestration, candidate history and email notifications.
- `candidate_lifecycle.py`: lifecycle and outcome evidence.
- `market_data_cache.py`: persistent cache serialization.
- `market_calendar.py`: exchange calendar and session classification.
- `static/history_dashboard.js`: standalone Home/history dashboard browser code, syntax-checked directly by CI.

## Market-data providers

Provider availability is configuration- and entitlement-dependent; a provider returning historical data does not mean its quote is fresh enough for an actionable signal.

- **Stocks:** Alpaca is configured through credentials and the `ALPACA_STOCK_DATA_FEED` setting (default IEX in the workflow); Stooq and Yahoo Finance are also present in the scanner code. Massive and Webull quote paths are optional and disabled by default unless their enable flags are set.
- **Crypto:** Kraken and Coinbase market-data paths are present. Kraken requests are paced and hourly OHLC data uses `HourlyCandleCache`.
- **Company/news context:** SEC filing and news/fundamental enrichment are separate from independent quote verification.

The scanner records provider activity and price-quality diagnostics. Preserve the distinction between verified live prices, last-known references, and unverified/watch-only prices.

## State files and retention

| File | Purpose |
| --- | --- |
| `alert_state.json` | Alert deduplication and daily notification state. |
| `paper_tracker.json` | Detailed candidate discovery and outcome history. The code retains records with a last-seen/first-seen timestamp inside a rolling 365-day window. |
| `paper_tracker_lifetime.json` | Cumulative recommendation and checkpoint aggregates intended to outlive detailed records. |
| `buy_signal_history.json` | BUY-signal history. |
| `paper_trading_state.json` | Separate simulated account, positions, ledger, and diagnostics. |
| `scan_diagnostics.json` | Latest scan diagnostics and provider/price-quality information. |
| `scan_metadata.json` | Latest attempt/completion metadata and audit summary. |
| `provider_usage_history.json` | Rolling provider-usage telemetry used to assess pacing, quotas, and scan duration. |
| `.cache/kraken_ohlc.json` | Persistent hourly Kraken candle cache, restored/saved through GitHub Actions cache. |

### State-size observation

At the 2026-11-19 audit, `paper_tracker.json` was approximately 9.88 MB and contained 414 candidate records (317 stocks, 97 crypto). Each record carries many snapshots and audit fields. The file is large because it preserves detailed research evidence, not because the tracker contains millions of candidates.

Do not simply delete fields or shorten the 365-day window to reduce size. First identify which dashboard, learning, audit, and outcome consumers depend on each field. Any compaction should preserve immutable discovery baselines, verified/unverified distinctions, checkpoint outcomes, and lifetime aggregates. A safer future design may split compact dashboard summaries from detailed archival evidence, with migration and round-trip tests.

## Dashboard routes

Authenticated dashboard routes include:

- `/` — Home.
- `/history`, `/history/data`, `/history/dashboard.js` — discovery and performance history.
- `/paper-trading`, `/paper-trading/data` — simulated account.
- `/discovery-performance` — discovery performance.
- `/momentum-evidence` — momentum evidence.
- `/market-breadth` — scanned-universe breadth.
- `/intelligence`, `/intelligence/data` — intelligence research.
- `/settings` — settings.

Compatibility aliases currently remain for `/customize` and `/research`, and for the corresponding research data route. Do not remove aliases until internal links and any external callers have been checked.

`/health` is the lightweight service health endpoint. The app uses HTTP Basic authentication for protected site routes, configured through Render environment variables.

## Configuration

### GitHub Actions secrets / variables

- Required secrets: `JARVIS_GMAIL_USER`, `JARVIS_GMAIL_APP_PASSWORD`, `JARVIS_ALERT_TO`.
- History sync: `JARVIS_HISTORY_SYNC_TOKEN`.
- Optional stock data: `ALPACA_API_KEY`, `ALPACA_API_SECRET`.
- Optional provider credentials: `MASSIVE_API_KEY`; Webull credentials are read by the optional Webull integration.
- Optional variable: `ALPACA_STOCK_DATA_FEED` (workflow default: `iex`).

### Render environment

- Required for protected dashboard access: `JARVIS_SITE_USERNAME`, `JARVIS_SITE_PASSWORD`.
- Optional GitHub history read integration: `JARVIS_GITHUB_READ_TOKEN`.
- Optional scanner/provider configuration includes `JARVIS_KRAKEN_OHLC_CACHE_PATH`, `MASSIVE_LIVE_QUOTE_ENABLED`, and `WEBULL_QUOTE_ENABLED`.

Never put secret values in the repository or documentation. The list above names variables only.

## Validation and release

The consolidated GitHub Actions validation workflow runs on relevant Python, dependency, test, workflow, and static-asset changes. It:

1. Compiles Python sources.
2. Runs the full `tests/` suite.
3. Extracts and syntax-checks the dashboard JavaScript.
4. Extracts and syntax-checks Intelligence page scripts.

The market-alert workflow also runs the regression suite before scanning. Render auto-deploys application-code changes; `render.yaml` excludes state and documentation files from triggering builds.

Recommended release sequence:

1. Make a focused change on a feature branch.
2. Add or update a regression test.
3. Run the full validation workflow.
4. Review the PR diff for accidental changes to state files or safety gates.
5. Merge only after checks pass.
6. Confirm Render is live when runtime code changed.
7. Verify the next scan's diagnostics before changing provider or schedule settings.

## Non-negotiable safety invariants

- No live order placement or brokerage execution.
- Every real trade remains subject to explicit user approval.
- Official BUY score thresholds and readiness gates are not changed as part of cleanup.
- Risk-Off continues to block official BUY signals.
- Experimental Risk-Off entries, if enabled, remain paper-only and separately labelled.
- Verified/fresh price and valid risk levels remain required for paper entries.
- The $250 paper account and its fee model are not reset during maintenance.
- Scan cadence is not changed without provider-usage and quota evidence.
