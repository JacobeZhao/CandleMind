# RL Profitability Recovery Plan

## Objective

The objective is not to guarantee profit. It is to find a strategy with a
causal, cost-adjusted edge that survives preregistered out-of-sample tests. PPO
is suspended until a simpler directional strategy proves that edge exists.

## Confirmed Root Causes

Five independent audits reviewed features, MDP semantics, econometrics,
execution, and strategy architecture. Three independently confirmed the main
timing defect: row-stride sampling made the 1-hour observation 55 minutes stale
at execution. The corrected decision frame now observes the final completed
5-minute bar of each hour, executes at the immediate next 5-minute open, and
marks the complete holding interval.

Training also reused one deterministic year-long episode. At 20,000 steps this
was only about 2.3 passes through the same chronology. Feature weakness remains
likely, but the rejected 3x3 run cannot isolate it from these defects.

## Target Architecture

```text
causal market and microstructure features
    -> frozen cost-aware directional alpha
    -> deterministic no-trade/risk/holding policy
    -> optional RL sizing OR continue/exit overlay
    -> next-base-bar execution ledger
```

The alpha layer emits side, expected net return, and uncertainty. The risk layer
sets a no-trade band, volatility-targeted size, minimum hold, cooldown, drawdown
limits, and prohibits direct flips. RL may optimize one action dimension only;
it cannot create or reverse direction.

## Experiment Ladder

### Stage 0: Qualify The Ledger

- Reconcile fixed long/short paths against an independent vectorized ledger.
- Verify decision timestamp, immediate next-open fill, full interval P&L,
  signed funding, terminal liquidation, and seeded episode starts.
- Require equity error below `1e-10` on synthetic paths and below 1 bp on real
  paths. Any mismatch stops all model research.

### Stage 1: Prove Alpha Without RL

Compare preregistered baselines: fixed trend score, 1h/4h/24h time-series
momentum, Ridge regression, LightGBM net-return regression, and calibrated
long/short opportunity models. Predict 24h and 72h next-open returns. Trade only
when the lower confidence bound exceeds `max(50 bps, 2x round-trip cost)`.

Use at least five non-overlapping three-month outer tests. Each outer fold uses
an 18-24 month training window, four inner two-month calibration folds, a
24-hour purge, and 48-hour embargo sensitivity. A single fixed cost-aware trend
rule is the primary comparator; flat, buy-hold, and short-hold remain secondary.

Stage 1 passes only if:

- pooled paired block-bootstrap 95% lower bound of excess return is above zero;
- at least 60% of OOS months and three of five symbols are profitable;
- at least 100 validation trades, profit factor at least `1.10`, and no fold
  supplies more than half of profit;
- performance remains positive at 2x costs;
- deflated Sharpe probability is at least `0.95` and PBO at most `0.20`.

### Stage 2: Freeze Risk And Holding

Compare fixed 50% exposure, inverse-volatility sizing, and 10-15% annualized
volatility targeting. Compare fixed 24h/72h exits with signal-decay and ATR
exits. Select one configuration on inner folds only. Cap exposure at 50%, halve
size after 10% drawdown, and stop entries at 15% drawdown.

### Stage 3: Isolated RL Ablation

Use randomized, seeded 30/60/90-day contiguous episodes and at least 20 episode
equivalents. Test either sizing or continue/exit, never both together. Required
ablations include no RL, shuffled alpha, delayed alpha, zero/base/2x costs, and
deterministic versus randomized episodes.

RL passes only if at least 6/9 OOS runs are profitable, two of three fold
medians beat the frozen deterministic policy, worst equity is at least `0.95`,
maximum drawdown is at most 15%, and paired monthly bootstrap confidence is 95%.

### Stage 4: Untouched Confirmation

Freeze code and parameters before one six-month confirmation and three months
of paper trading. Historical periods already inspected during this research are
not eligible as untouched confirmation data.

## Data Priorities

Current G-drive data can test multi-timeframe trend, ADX, Hurst, volatility,
volume, VWAP deviation, taker ratio where populated, sparse 4h entries, and
24-168h holds. Higher-value additions are historical spread, L2 depth, signed
trade imbalance, open interest, liquidations, mark-index basis, predicted
funding, and crypto breadth. New data must show incremental OOS value over the
technical baseline before entering PPO observations.

## Immediate Run

When G drive is mounted, run the timing/IC audit before any retraining:

```powershell
python -m backend.scripts.evaluation.audit_rl_decision_timing `
  --data-root G:\CandleMind\CandleMind_data `
  --symbol BTCUSDT --start 2022-01-01 --end 2026-06-07 `
  --decision-interval-bars 12 --horizon-hours 1 4 24 `
  --json-out G:\CandleMind\CandleMind_data\experiments\reports\rl_timing_audit_BTCUSDT.json
```

No PPO training is authorized until Stage 0 and Stage 1 pass.

## Execution Status

Stage 0 timing reconciliation passed. Stage 1 failed under standard costs, 2x
costs, nested sparse-momentum selection, and bootstrap stability tests. See
`RL_PROFITABILITY_RECOVERY_RESULTS.md`. PPO remains suspended until new
data produces a qualifying non-RL alpha baseline.
