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
