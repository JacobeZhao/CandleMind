# PPO Next-Open V2 Experiment

> **Superseded on 2026-07-13.** A composition-level timing audit found that
> `iloc[::12]` retained one 5-minute candle per hour and then executed at the
> next sampled open. Observations were therefore 55 minutes stale at execution.
> The rejection remains valid for these model artifacts, but this experiment
> does not establish the performance of a correctly aligned 1-hour strategy.

## Scope

This experiment corrects the RL execution and evaluation semantics before any
further PPO tuning. The environment now observes completed bar `i`, executes
the target change at `open[i+1]`, applies signed funding, and charges a final
exit cost for terminal positions. `market_v2` has no probability shaping.
Walk-forward ranges are half-open and test features receive 35 days of causal
warm-up history.

## Configuration

- Symbol/features: `BTCUSDT`, `market_v2`
- Policy: target-position PPO (`short`, `flat`, `long`)
- Frequency/exposure: 1-hour decisions, 50% position, 24-hour maximum hold
- Costs: 10 bps fee plus 2 bps slippage per turnover unit
- Funding baseline: explicit `0.0` per 8 hours
- Discounting: 24-hour half-life (`gamma=0.9715319412`)
- Training: 20,000 steps, no pretraining, seeds 42/43/44
- Validation: three 12-month train / roughly 2-month OOS folds

## Results

| Fold | PPO median | PPO worst | Best comparator | Median trades |
| --- | ---: | ---: | ---: | ---: |
| 1 | 0.9759 | 0.8896 | 1.0553 buy-hold | 42 |
| 2 | 0.8682 | 0.8567 | 1.1032 short-hold | 54 |
| 3 | 0.8769 | 0.8164 | 1.0487 short-hold | 53 |

Across all nine OOS runs, none was profitable and no fold median beat its best
flat/buy-hold/short-hold comparator. Seed mean equities were `0.9333`, `0.8869`,
and `0.9034`. All promotion gates failed.

## Decision

Status: **rejected; do not promote**. The models lose under their implemented
environment and remain rejected. Because the hourly timing composition was
wrong, the losses cannot isolate feature quality, reward quality, or PPO
capacity. Do not rerun PPO until the corrected decision ledger, randomized
training episodes, and a profitable non-RL alpha baseline pass their gates.
