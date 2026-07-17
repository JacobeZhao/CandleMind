# RL Profitability Recovery Results

## Executive Decision

The current BTC feature set does not support a statistically defensible,
cost-adjusted directional strategy. PPO training remains suspended. This is a
stage-gate failure, not evidence that a larger policy or longer run will help.

Five independent agents audited feature information, labels, MDP/PPO design,
econometrics, execution costs, and strategy architecture. Their shared
recommendation was to prove alpha outside RL and use RL only for one isolated
risk or execution decision after that proof.

## Platform Finding

The previous 1-hour experiment sampled rows with `iloc[::12]`. An observation
from the first 5-minute candle of an hour executed at the next sampled open,
making it 55 minutes stale. The new decision frame observes the last completed
5-minute candle, executes at the immediate next 5-minute open, and accounts for
the complete interval. Synthetic reconciliation and 24 local tests pass.

On 38,831 real BTC decision points, corrected timing changed the 1-hour IC of
`return_1` from `+0.0112` to `-0.0441`. Most slower features were constant
within the hour and therefore did not materially change.

## OOS Evidence

Five fixed three-month outer tests used 24-month rolling training windows,
next-open fills, 50% exposure, and 10 bp fee plus 2 bp slippage per one-way
turnover.

| Baseline | Final equity | Profitable folds | Trades | Profit factor | Bootstrap lower bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| 24h momentum | 0.8286 | 2/5 | 332 | 0.885 | -0.00175 |
| 1h return reversal | 0.9272 | 1/5 | 91 | 0.693 | -0.00176 |
| Ridge 24h | 0.7953 | 1/5 | 172 | 0.765 | -0.00294 |
| Ridge 72h | 0.7224 | 2/5 | 136 | 0.742 | -0.00541 |
| Fixed trend 72h | 0.8210 | 2/5 | 129 | 0.828 | -0.00482 |

A nested 36-candidate sparse-momentum experiment selected thresholds, holding
periods, entry frequency, and trend agreement using four inner folds only. It
produced `0.8801` equity at base costs and `0.7832` at 2x costs. The confidence
interval included zero and two outer-fold selections had no qualified inner
candidate.

## Cost Frontier

The only positive gross signal was fixed 24-hour momentum:

- Zero cost: equity `1.2341`, PF `1.186`, 3/5 profitable folds.
- 5 bp one-way cost: equity `1.0454`, PF `1.050`, 3/5 profitable folds.
- 12 bp one-way cost: equity `0.8286`, PF `0.885`, 2/5 profitable folds.

Even at zero cost its bootstrap lower bound remained negative. Lower fees alone
cannot meet the stability gate.

## Data Audit

The higher-timeframe taker and funding defects were traced and repaired. Stale
normalized Parquet rows won duplicate timestamps during refresh, while funding
milliseconds were compared with nanosecond timestamps. Funding statistics were
also incorrectly computed on repeated 5-minute rows instead of 8-hour events.

The rebuilt 466,177-row feature set has greater than 99.98% non-null coverage
for 30m, 1h, and 4h taker ratios. Funding now has 1,285 distinct rates and is
available for 32.3% of the history. The corrected audit found:

- `5m_vwap_dev_r`: 1h IC `-0.0703`, but only a `3.86 bp` decile spread.
- Taker features pass some 1h/4h statistical checks, but their spreads remain
  below trading costs.
- Funding has a full-sample 72h decile spread near `124 bp`, but only from 2025
  onward and after full-sample inspection.

A 168-candidate nested cost-hurdle audit therefore re-estimated every direction
and threshold inside each training fold. No candidate passed the 2x-cost inner
gate in any outer fold, so all five folds correctly stayed flat. The closest
training result was a `40.6 bp` gross lower confidence bound against the
pre-registered `48 bp` hurdle; later windows were weaker or negative.

## Executable Next Strategy

Do not run PPO on the current data. The next research iteration must:

1. Collect synchronized spread, L2 imbalance, open interest, liquidations,
   mark-index basis, and breadth; taker/funding generation is now repaired.
2. Measure actual fill-level fee and slippage distributions rather than assume a
   cheaper tier. Keep 12 bp and 2x costs as stress scenarios.
3. Train a 24h/72h cost-hurdle return model on nested folds. It may emit a side
   only when the lower forecast bound exceeds 2x expected round-trip cost.
4. Freeze deterministic volatility sizing and holding rules before any RL test.
5. Permit RL to optimize sizing or continue/exit only if the frozen alpha has a
   positive 95% block-bootstrap lower bound, PF at least `1.10`, at least 60%
   profitable OOS months, and positive results at 2x costs.
6. Use new paper-trading data for confirmation because the existing historical
   periods have now been repeatedly inspected.

No model from this work is approved for live trading or promotion.

## Artifacts

- `G:\CandleMind\CandleMind_data\experiments\reports\rl_timing_audit_BTCUSDT.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_alpha_baseline_BTCUSDT_v1.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_nested_momentum_BTCUSDT_v1.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_microstructure_feature_audit_BTCUSDT.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_alpha_baseline_BTCUSDT_zero_cost.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_alpha_baseline_BTCUSDT_5bp_one_way.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_microstructure_feature_audit_BTCUSDT_v2.json`
- `G:\CandleMind\CandleMind_data\experiments\reports\rl_cost_hurdle_alpha_BTCUSDT_v1.json`
