# Jarvis Market Scanner

Jarvis is a research-focused stock and cryptocurrency scanner with a web dashboard, market-data diagnostics, early-runner research features, and an isolated paper-trading simulator.

**Safety boundary:** Jarvis does not place live orders. Scanner stages and alerts are research outputs. The paper account is simulated; any real trade remains subject to the user's separate review and approval.

## What runs where

- **GitHub Actions** runs the scheduled scan. The `Jarvis Market Alerts` workflow is dispatched by the external cron-job.org schedule; it is not configured with a GitHub cron schedule.
- **Render** hosts the FastAPI dashboard and reads the latest synchronized/committed history.
- **Paper trading** runs inside the scan workflow and updates its own state. It is separate from live brokerage execution.

## Main capabilities

- Stock and crypto universe discovery and scanning.
- Price freshness and independent verification checks.
- Market-regime and setup classification.
- Early-runner and momentum research features.
- Candidate lifecycle and discovery-performance tracking.
- Strict BUY-readiness gates and separate Risk-Off paper diagnostics.
- $250 paper-trading account with fee-aware simulated accounting.
- Dashboard pages for history, paper trading, discovery/performance, momentum evidence, market breadth, and intelligence.

## Repository guide

| Path | Responsibility |
| --- | --- |
| `main.py` | FastAPI app, market-data providers, stock/crypto scans, scoring/classification, dashboard pages and API routes. |
| `send_alerts.py` | Scheduled scan orchestration, alert selection/email, candidate history, paper-tracker updates, and dashboard history sync. |
| `early_runner.py` | Research-only early-runner, breadth, catalyst, and data-quality features. |
| `buy_intelligence.py` | BUY-readiness and score-blending logic. |
| `runner_intelligence.py` | Runner intelligence report construction. |
| `paper_trading.py` | Simulated account, fee calculation, paper entries/exits, and account summary. |
| `candidate_lifecycle.py` | Candidate lifecycle and outcome tracking. |
| `market_calendar.py` | Exchange-session calendar helpers. |
| `market_data_cache.py` | Persistent hourly candle cache. |
| `static/history_dashboard.js` | Home/history dashboard browser code served by `/history/dashboard.js`. |
| `templates/history.html` | Home/history dashboard HTML. |
| `templates/intelligence.html` | Intelligence dashboard HTML. |
| `templates/analytics_tab.html` | Shared Discovery Performance, Momentum Evidence, and Market Breadth page template. |
| `templates/settings.html` | Settings page template. |
| `templates/paper_trading.html` | Paper Trading page template. |
| `tests/` | Regression tests for scanner components and paper trading. |
| `.github/workflows/jarvis-alerts.yml` | Scheduled scan workflow, cache restore, scan audit, and state persistence. |
| `.github/workflows/validate-jarvis.yml` | Consolidated code-validation workflow. |
| `render.yaml` | Render web-service and build-filter configuration. |

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Local development

Use Python 3.12.

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
python -m compileall -q .
python -m pytest -q tests
```

The dashboard JavaScript checks are included in the GitHub Actions validation workflow.

## Important operating rules

1. Do not relax official BUY thresholds or BUY-readiness checks without an explicit strategy review and regression tests.
2. Risk-Off paper experiments must remain separately labelled and isolated from official BUY signals.
3. Do not allow stale, unverified, or watch-only prices to become BUY-eligible.
4. Never add live brokerage order placement to the scanner workflow.
5. Do not change the external scan cadence without reviewing provider quotas, request pacing, cache behavior, and recent failure history.
6. Treat historical performance as observational; it is not a promise of future returns.

## Configuration

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#configuration) for the environment-variable inventory. Secret values belong in GitHub Actions or Render environment settings, never in source control.
