# SOL SAR + ADX Structural Optimization V1

## 文档状态

这是冻结的历史研究证据。其 V3 参数仍由当前 paper runtime 和离线回测使用，
但结论仍是未盈利、不可作为生产准入依据。跨品种页面回测复用 SOL 参数，结果
只用于诊断，不表示已完成对应品种调优。

## Fixed Skeleton

The experiment keeps the requested structure unchanged:

- 5-minute Parabolic SAR (`0.02`, `0.20`) for setup and stop events
- completed 1-hour `ADX(14)` plus `+DI/-DI` for trend permission
- 20% initial target exposure and at most four additional 20% layers
- next-5-minute-open execution
- 10 bps fee, 2 bps slippage, and observed signed funding

Only entry maturity, ADX strength, same-regime re-entry limits, and recapture
buffer were adjusted.

## Structural Changes

1. Require ADX to rise for two completed 1-hour observations before adding risk.
2. Require the aligned 5-minute SAR state to survive multiple completed bars.
3. Require recapture to exceed the latest fill by 0.24% and reject a new layer
   whose actual fill does not progress in the profitable direction.
4. Limit first entries within one continuous ADX trend regime.

ADX rising and DI strength gate entries and adds only. Existing positions are
not closed merely because ADX stops rising; exits remain SAR reversal, base ADX
loss, DI direction change, PIT universe exit, or test end.

## Results

| Version | Development | 2026H1 | Notes |
|---|---:|---:|---|
| ADX40 baseline | -68.83% | -23.25% | Unlimited SAR re-entry |
| V2 maturity gates | -21.88% | -3.90% | 420 / 92 cycles |
| V3 regime entry cap | -2.19% | -0.95% | 123 / 30 cycles |

V3 selected `ADX >= 45`, six 5-minute confirmation bars, at most two entries
per ADX regime, and a 0.24% recapture buffer. Development Profit Factor was
`0.850`; reused 2026H1 Profit Factor was `0.684`. It is not profitable.

Two lower-frequency development candidates crossed breakeven but failed time
stability:

| Candidate | 2024 | 2025 | Reused 2026H1 | Cycles in 2024-2025 |
|---|---:|---:|---:|---:|
| ADX50, 12-bar confirm, one entry | +0.33% | -0.18% | -0.79% | 33 |
| ADX55, 6-bar confirm, one entry | -0.75% | +2.32% | -0.89% | 23 |

The apparent full-development profits come from one year and do not generalize.
They must not be promoted.

## Evidence And Decision

- V2: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/sol_adx_sar_structural_sweep_v2_202401_202606_v1`
- V3: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/sol_adx_sar_regime_entry_sweep_v3_202401_202606_v1`
- V3 manifest: `191c241c29bdd39ea8fa24b48a8f439e6d9ffcc99b84f4f4272f165c405d3846`

The fixed skeleton now has positive gross PnL in some windows, but the edge is
too small and unstable to cover execution costs. Further threshold mining on
the inspected history is not justified. A future untouched release and an
independent mature-engine ledger comparison are required before another
admission decision.
