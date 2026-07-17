# RL Strategy V2 Optimization Report

Date: 2026-07-12

## Decision

No RL candidate is approved for production. The corrected promotion decision is `fail`.
The best action is to keep live trading disabled for these candidates and retain them only
as reproducible research artifacts.

## Root Causes Found

1. Multi-horizon labels used features from bar `i` but entered at `open[i]`. Correct causal
   execution is the next tradable price, `open[i + 1]`. The correction changed 16-19% of
   BTC labels.
2. The old V2 result (`AUC about 0.76`, `IC about 0.43`) collapsed after correction. BTC 1h
   causal barrier models scored about `AUC 0.52`, proving that the earlier result was inflated.
3. Barrier labels for 30m, 1h, and 4h all had a median duration of three 5m bars. They were
   not genuinely multi-horizon trend targets.
4. RL reward charged transaction costs through equity and then penalized the same cost again.
5. Total drawdown was penalized every step instead of penalizing only new drawdown.
6. The old action environment charged two turns when closing a position.
7. The walk-forward baseline interpreted the first two `market_v2` features as probabilities,
   creating a false baseline and an incorrect promotion `pass`.

## Implemented Corrections

- Causal next-open labels and cost-aware terminal-return labels.
- Purged temporal validation boundaries.
- Training-only gain feature selection instead of raw-scale variance selection.
- Net log-equity reward with incremental drawdown penalty.
- Correct one-way close cost and explicit funding cost.
- Pure `market_v2` observation set with no supervised probability columns.
- Configurable decision interval and 50% default experiment exposure.
- Explicit flat, buy-and-hold, and short-and-hold comparators.
- Cross-symbol and high-cost stress-test tooling.

## Results

BTC 1h cost-aware classifier reached `AUC 0.614` and `IC 0.142`, but its highest-confidence
10% long trades averaged `-0.256%` net and short trades averaged `-0.116%` net. Continuous
1h regression also failed. A 4h regression showed isolated short-side value, but six-fold
walk-forward equity remained below 1.0.

PPO walk-forward results versus an explicit flat baseline:

| Decision frequency | Fold equities | Outcome |
| --- | --- | --- |
| 5m, corrected costs | 1.007 / 0.884 / 0.798 | Fail |
| 1h | 0.946 / 0.994 / 0.853 | Fail |
| 4h | 0.951 / 0.842 / 0.908 | Fail |

Cross-symbol 4h PPO mean equity under standard costs:

| Symbol | Mean equity | Profitable folds |
| --- | ---: | ---: |
| ETHUSDT | 0.886 | 0/3 |
| SOLUSDT | 0.940 | 1/3 |
| BNBUSDT | 0.944 | 0/3 |
| XRPUSDT | 0.893 | 0/3 |

Higher-cost stress reduced every result further.

## Artifact Locations

- Reports: `G:/CandleMind/CandleMind_data/experiments/reports/`
- RL candidates: `G:/CandleMind/CandleMind_data/models/rl/candidates/`
- Causal/trend labels: `G:/CandleMind/CandleMind_data/processed/labels/`
- Primary reports:
  - `rl_market_v2_walk_forward_BTCUSDT_reevaluated.json`
  - `rl_market_v2_1h_walk_forward_BTCUSDT.json`
  - `rl_market_v2_4h_walk_forward_BTCUSDT.json`
  - `rl_market_v2_4h_cross_symbol_stress.json`
  - `trend_walk_forward_BTCUSDT_4h_trend_v1.json`

## Next Research Gate

Do not spend more compute on the current feature matrix. Resume RL research only after adding
new causal information such as order-book imbalance, open-interest change, liquidation flow,
maker/taker execution state, and cross-sectional relative strength. A new signal must first
show positive non-overlapping net returns in at least 60% of monthly walk-forward folds. RL
must then beat flat and directional hold baselines after stressed costs before promotion.
