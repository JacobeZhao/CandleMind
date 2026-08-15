# SAR + ADX Backtrader Validation V1

## 文档状态

这是冻结的跨引擎一致性证据。它证明确定性账本与 Backtrader 在指定数据和成本
假设下结果一致，但不证明策略盈利，也不覆盖盘口冲击、延迟和交易所故障。

## Purpose

The SOL SAR/ADX strategy was independently replayed through Backtrader
`1.9.78.123`. Backtrader owns order scheduling, fills, positions, commission,
slippage, cash, trade PnL, and marked equity. The repository strategy adapter
owns only the frozen decision state machine and funding cash-flow injection.

Market orders use the previous completed 5-minute decision and execute at the
current open through Backtrader's open phase. Percentage slippage is enabled on
open fills. Funding events are rounded forward to the first 5-minute open not
earlier than `available_at`, settled against the pre-order position, and added
to broker cash before same-open orders.

## Frozen Configuration

- Symbol: `SOLUSDT`
- 5-minute Parabolic SAR: `step=0.02`, `max=0.20`
- 1-hour ADX: period `14`, threshold `45`
- ADX rising periods: `2`
- SAR confirmation: `6` completed 5-minute bars
- Maximum entries per ADX regime: `2`
- Layers: five equal 20% tranches
- Recapture buffer: `0.24%`
- Fee: `0.10%` per fill
- Slippage: `0.02%` per fill

## Cross-Engine Parity

| Metric | Custom ledger | Backtrader | Difference |
|---|---:|---:|---:|
| 2024-2025 final equity | 9781.341615675570 | 9781.341615675568 | < 0.00000000001 |
| 2024-2025 cycles | 123 | 123 | 0 |
| 2024-2025 adds | 34 | 34 | 0 |
| 2024-2025 fees | 629.205276710779 | 629.205276710779 | 0 |
| 2024-2025 funding | -1.845163232357 | -1.845163232357 | 0 |
| 2026H1 final equity | 9905.272109501868 | 9905.272109501868 | 0 |
| 2026H1 cycles | 30 | 30 | 0 |
| 2026H1 adds | 14 | 14 | 0 |
| 2026H1 fees | 174.263879900906 | 174.263879900906 | 0 |
| 2026H1 funding | 0.181704475763 | 0.181704475763 | 0 |

The independent broker engine confirms the custom ledger's economic result.
The strategy remains unprofitable: `-2.19%` in 2024-2025 and `-0.95%` in the
already-inspected 2026H1 window. Framework replacement does not create an edge.

## Evidence

- Development: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/backtrader_sol_v3_202401_202512_v2`
- Reused 2026H1: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/backtrader_sol_v3_2026h1_v2`
- Development manifest: `bbe07f37a530e8d74cb5b54a62a26011e2b29015e16785baf87651d11d3e348a`
- 2026H1 manifest: `78261628084e866f6bca9d5c6a8715b03a109bb1f08e5433c469275e6a9b7a82`

These releases are diagnostic and must not be promoted. Backtrader itself does
not model order-book queue position, liquidity impact, liquidation, exchange
outages, or latency from 5-minute OHLC alone.
