# SAR + ADX Pyramiding Baseline V1

## Strategy Contract

This diagnostic strategy uses completed 1-hour bars to calculate Wilder
`ADX(14)`, `+DI`, and `-DI`. Trading is enabled only when `ADX >= 25`.
`+DI > -DI` permits long positions; `-DI > +DI` permits short positions.
Equal DI values and sub-threshold ADX are non-tradable.

Within an allowed regime, 5-minute Parabolic SAR (`0.02`, `0.20`) supplies
entry and stop/reversal events. A completed-bar signal executes at the next
5-minute open. A countertrend SAR reversal closes the position but does not
open the prohibited direction. Loss of the ADX/DI regime also closes the
position at the next open.

The initial entry is 20% of target notional. For a long, a close below the
latest layer fill arms an add; a later close above that fill adds another 20%
at the next open. Shorts are symmetric. Each fill becomes the new anchor and
at most five layers are allowed. SAR reversal takes priority over adding.

## Data And Execution

- Window: `[2024-01-01, 2026-07-01)`
- Universe: monthly point-in-time Top 20 from the verified 30-symbol release
- Portfolio: 10,000 USD split into 30 fixed equal-weight subaccounts
- Fee: 10 bps per fill
- Slippage: 2 bps per fill
- Funding: observed, signed funding events
- Fill timing: completed signal bar, immediate next open

This is development evidence, not untouched confirmation evidence. Spread and
market-impact observations are not present in the release, so the result is
not a production admission test.

## Results

| Metric | Unfiltered SAR | 1h ADX/DI Filter |
|---|---:|---:|
| Total return | -96.69% | -86.32% |
| Final equity | $331.10 | $1,367.87 |
| SAR cycles | 460,632 | 115,542 |
| Adds | 370,005 | 83,792 |
| Win rate | 21.92% | 21.42% |
| Profit factor | 0.505 | 0.467 |
| Fees | $8,186.15 | $6,773.03 |
| Profitable symbols | 0 / 30 | 0 / 30 |

ADX filtering removed about 75% of cycles and materially reduced the loss, but
the remaining 5-minute SAR turnover still overwhelms gross edge. The strategy
must not be promoted or traded.

## Evidence

- Unfiltered: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/sar_5x20_pit_202401_202606_base_v1`
- ADX-filtered: `G:/CandleMind/CandleMind_data/experiments/sar_pyramid/adx14_25_sar_5x20_pit_202401_202606_base_v1`
- Filtered manifest: `6aa7b86fbb3c73a6d49e14c366175e5596ea527060f4ec3d19edda9e472c7d4c`

The next bounded experiment should keep this ledger fixed and compare slower
SAR configurations or a 15-minute SAR execution signal. ADX threshold tuning
alone is unlikely to solve the observed turnover problem.
