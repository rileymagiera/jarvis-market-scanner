# GitHub high-win-rate research and Jarvis paper-only confluence pilot

## Purpose

Compare externally advertised win rates with reproducible evidence and test
adapted ideas in Jarvis's existing paper model, not its official BUY engine.

- https://github.com/erhungn/volatility-regime-momentum: reports 60.3% wins over 378 backtested trades with profit factor 1.74. Describes out-of-sample checks, an ATR-normalized momentum entry and a soft ATR expansion filter. Instruments and exits differ from Jarvis; these figures are third-party claims, not verified Jarvis outcomes.
- https://github.com/crollila/strategy-survival-funnel: walk-forward testing of 9,222 strategy/asset configurations; emphasizes sample size, out-of-sample validation, bootstrap robustness, and net-of-cost comparisons. A validation reference, not a high-win-rate trading model.
- https://github.com/freqtrade/freqtrade: documents lookahead-bias audits. Adopt its principle of causally available snapshots, not its dependency stack.
- https://github.com/Hritish-Narayanan/Trading-with-a-ml: claims 82.82% across 37,450 simulated transactions. The inspected engine credits a high-of-day profit target before checking a low-of-day stop, and some fills use complete-day values. We do not copy this misleading fill logic.

## Duplicate check

Jarvis already has scanner momentum, relative strength, accumulation, volatility
contraction, Bollinger squeeze expansion, early-runner detection, strict risk and
price-quality gates, fee-aware paper trades, and grouped strategy outcomes.
Do not duplicate those features.

## Implemented paper-only pilot

paper_edge_research.py combines existing signals into versioned evidence
cohorts, with unknown inputs remaining unknown:

1. Trend alignment (existing EMA stack or multi-timeframe alignment).
2. Soft volatility expansion (existing Bollinger squeeze expansion or
   ATR contraction <= 5%, the soft 0.95 ratio analogue).
3. Positive stock 5-day or crypto hourly/24-hour momentum.
4. Volume participation (existing RVOL / acceleration measurements).
5. Relative strength (stocks vs SPY/IWM, crypto vs BTC), plus accumulation
   as secondary cohort labels.

An experimental early-entry candidate with all four of the primary confirmations
is ranked before otherwise eligible nonconfluence experimental early-entry
candidates. Existing official BUY ranking, Risk-Off ranking, signal eligibility,
price verification, $250 paper capital, stop/target logic, risk limits, fees,
maximum four positions, and scan scheduling are unchanged.

The profile is stamped on every new eligible paper entry, including official
BUYs, so longitudinal cohort reporting compares closed trades only. Each
cohort reports count, win rate, net modeled P/L, profit factor, and expectancy
after modeled costs. Pre-existing trades without a profile are excluded rather
than relabeled with hindsight. The dashboard shows current verified-candidate
confluence coverage and cohort results.

## Evaluation rules before considering promotion

- Never interpret a repository's self-reported win rate as Jarvis's edge.
- No next-scan result can establish a higher win rate; accumulate enough
  closed trades in separate time windows, with distinct stock/crypto reviews.
- Compare win rate and expectancy, profit factor, fees, drawdowns,
  risk-adjusted returns, fill realism, blocked candidates, and missed runners.
- Freeze pilot factors before later walk-forward / holdout evaluation.
- Do not infer stop vs target order from an aggregated candle.
- Treat sampled exits as estimates, not guaranteed market executions.
- Keep this experiment paper-only, with no change to official BUY eligibility.


## Fee-adjusted paper trade-quality enhancement

The first two closed Risk-Off crypto paper positions were MEGAUSDT and
GRASSUSDT. Their saved trade ledgers show approximately 23.2% and 20.0%
of the respective net losses were estimated transaction costs. This
two-trade sample cannot estimate future win rate, but clearly
illustrates the importance of comparing projected payoffs net of fees.

The paper simulator now computes a **net target-ladder reward/risk** using:
- exactly the existing broker fee/spread assumptions;
- the actual verified candidate entry and saved stop and targets;
- 50% sale at target 1 and 50% at target 2 (idealized winning path);
- a full stop exit (idealized losing path);
- a normalized hypothetical $100 notional to avoid rounding bias for
  small-price tokens (not a simulated position allocation).

For *experimental* paper signals only, this net reward/risk is an additional
tie-breaker after all existing score and target-2 gross R/R priorities.
Official BUY eligibility, thresholds, and main score order are untouched.
The existing $250 balance, fee estimates, risk sizing, stop/target levels,
and timing remain exactly the same.

The new dashboard fields show estimated **net target ladder R/R** and
actual **modeled fees on closed trades**. The latter are observational
and already included in the paper account's realized P/L.

**Important:** Net ladder reward/risk is *not* a win probability or
expected return. A simulated position may reach target 1 then stop out,
or hit a stop between scans, so realized outcomes can differ. Current
Coinbase commission beyond the included monthly allowance remains
unconfigured; actual future results may be worse. Do not extrapolate
win-rate improvements until enough versioned trade outcomes exist.

## Additional open-source review — November 19, 2026

### Source comparison and *new* ideas versus existing Jarvis functionality

| Project | Source evidence | Already in Jarvis | Remaining experiment |
| --- | --- | --- | --- |
| [erhungn/volatility-regime-momentum](https://github.com/erhungn/volatility-regime-momentum) | Self-reports 60.3% of 378 backtest trades and PF 1.74, with OOS analysis; primary assets were gold, FX, oil, and indices, not Jarvis's stock/crypto universe. | Paper confluence v1 already blends trend, soft volatility expansion, momentum and volume. | Separately test **ATR-normalized momentum** and hold-time sensitivity on Jarvis's own closed and future trades; do not alter stops or exit strategy automatically. |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | Research tooling includes rule-significance tests, Monte Carlo and parameter robustness checks. | Versioned frozen signal snapshots and basic grouped closed-trade expectancy. | Add confidence-aware, regime-separated actual paper-trade diagnostics, then forward-only walk-forward testing. |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | Dedicated lookahead and indicator recursion diagnostics. | Frozen entry snapshots and source freshness guards reduce some hindsight errors. | Add automated test to ensure each feature was available at the signal timestamp and no later candle contaminated it. |
| [crollila/strategy-survival-funnel](https://github.com/crollila/strategy-survival-funnel) | Author reports 67/9,222 configuration survivals under OOS filters, trade-count and bootstrap checks. The result applies only to that study. | Jarvis has signal-lead-time audit and requires fresh independent quote timestamps. | Require untouched chronological validation and net-of-cost expectancy before promoting an experimental rule. |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | Models market order fills, order-book data, latency, slippage and fill uncertainty. | Jarvis models some exchange costs and independently verifies current prices; exits are sampled at scan time. | Compare baseline exits against worse-price, delayed-exit and missed-fill **shadow scenarios**, especially on fast-moving small-cap stocks and volatile crypto. |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Research portfolios expose transaction fees, slippage and fill handling as explicit assumptions. | Jarvis already has fee-aware paper accounting. | Stress test costs and execution assumptions with unchanged original ledger. |
| [ranaroussi/quantstats](https://github.com/ranaroussi/quantstats) | Calculates win rate, profit factor, drawdown and risk-adjusted return. | Jarvis already reports win rate, P/L, expectancy and profit factor for entry cohorts. | Report uncertainty and stratification rather than duplicate basic scorecards. |
| [Hritish-Narayanan/Trading-with-a-ml](https://github.com/Hritish-Narayanan/Trading-with-a-ml) | Advertises >80% simulated success, but high-frequency simulation checks same-day profit targets before same-day stops while relying on completed daily high/low values. | Jarvis exits use verified scan observations rather than optimistic same-bar highs. | Reject target-first optimistic fill rules; use a conservative ambiguous-bar policy in any future bar-level backtester. |

### Verified Jarvis situation at this review

- Read from GitHub `paper_trading_state.json` with `last_updated_at=2026-11-20T01:04:26.466664+00:00` (8:04 p.m. Eastern November 19).
- Shared experimental paper account remained at **$245.2690803** of $250 starting cash, with **zero open positions**.
- The only two closed trades were Risk-Off experimental crypto entries: MEGAUSDT **-$2.14328163** and GRASSUSDT **-$2.58763807** after modeled costs. These are **two observations, not evidence of a stable 0% win rate**.
- The recorded 238 Risk-Off candidates had zero eligible fresh signals; the separately enabled early-entry experiment also had zero eligible candidates because all 238 had Risk-Off/unknown regime.
- **Do not** reset ledger, change risk limits, disallow/allow a regime, or reduce official BUY thresholds based on these two outcomes.

### Priority roadmap (new versus existing)

1. **Research verification, additive and safe:** `paper_trade_validation.py` provides Wilson intervals, entry-regime × strategy/asset breakdown, closed-P/L drawdown and explicit insufficient-sample notices. Exposed read-only by `account_summary()`. This does not change trade selection.
2. **Forward-only evidence gate, not implemented yet:** freeze challenge strategy definitions before a later chronological holdout. Apply market-regime splits, asset cohorts, costs, confidence bounds, and sample-size warnings. Keep candidates with missing features unknown, not automatically rejected as failures.
3. **Shadow execution-stress scenarios, not implemented yet:** apply hypothetical adverse fill/slippage and scan-delay scenarios off the recorded baseline. Do not rewrite historical fills or adjust Coinbase/Webull cost assumptions in the official paper engine without approval.
4. **Counterfactual missed-runner evaluation, not implemented yet:** compare verified early signals, actual eligible entry ranges, later maximum favorable/adverse paths, and BUY-gate reasons. Candidate +20% future movement alone does not establish a profitable executable trade.
5. **ATR-normalized momentum challenger, not implemented yet:** build a named shadow-only variant of the current confluence pilot using a correctly time-aligned ATR signal. Never confuse source-study parameters with proven universal settings.
6. **Risk-Off comparison, not implemented yet:** evaluate whether avoiding new experimental entries in Risk-Off helps on future out-of-sample observations. Preserve the existing experimental configuration until a separately approved strategy change.

### Honest interpretation and promotion criteria

The objective is profitable, repeatable decisions **after actual trading costs**, not maximizing the ratio of tiny winners to occasional large losses. Compare official BUY, Risk-Off experimental, early-entry experimental, and future shadow variants separately. Do not promote a strategy based on the source author's headline win rate, two closed Jarvis trades, lookahead-contaminated candles, unrepresentative markets, or cherry-picked winners. A 30-trade diagnostic label is *not* a statistically validated promotion threshold. Deployment and risk/strategy changes require the project's test and approval safeguards.

This is a selective source review, **not an exhaustive crawl of all GitHub trading repositories**.
