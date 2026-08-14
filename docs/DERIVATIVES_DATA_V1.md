# Derivatives Data V1

## Decision

The V1 collection and normalization pipeline passes source, causality, coverage,
atomicity, and integrity acceptance. This is data-infrastructure evidence only;
it does not show incremental alpha or make V3/V4 deployable.

## Source Contract

| Dataset | Binance Vision source | Archive | Causal availability |
| --- | --- | --- | --- |
| Open interest | `metrics` | daily | source timestamp plus 5 minutes |
| Basis | mark/index/premium 5m K-lines | monthly plus boundary days | latest source close plus 1 ms |
| Funding | `fundingRate` | monthly | actual settlement timestamp |
| Depth | `bookDepth` | daily | completed 5-minute bucket |

OI includes total contracts/value and published account/taker ratios. Basis is
`mark_close / index_close - 1`; the premium index is retained as an independent
field. Depth contains aggregate notional at +/-1% through +/-5%, not historical
L2 quotes or measured spread. Tested `liquidationSnapshot` and `bookTicker`
archive paths return 404 and are not synthesized.

## Acceptance Release

`derivatives_v1_acceptance_30_20250101_20260726` covers all 30 canonical symbols
for 2025-01-01. It contains 120 Parquet outputs and 26,013 rows: 8,640 each for
OI, basis, and depth, plus 93 funding events. Every 5-minute dataset has 288
rows per symbol; depth has ten snapshots per bucket. The manifest self-hash and
all 120 output hashes pass.

Manifest SHA-256:
`9adea42ed83fc7e55f07c779c1f922b79c3e7693e4fb2f2f48ba388788289f3a`.

The earlier BTC-only acceptance remains immutable but is superseded because its
OI timestamp dtype was inferred as floating point. Use the 30-symbol release,
which enforces integer millisecond timestamps.

## Observed Funding Release

`funding_observed_30_20240101_20260630_v1` is the immutable funding-only
release for EMA research. It covers all 30 canonical symbols from 2024-01-01
through 2026-06-30 using 900 checksum-verified monthly Binance Vision archives.
The release contains 30 normalized Parquet outputs and 84,815 observed funding
events. Every output starts on 2024-01-01, ends on 2026-06-30, and has a maximum
observed event gap no greater than 8.000005 hours.

Manifest SHA-256:
`8b215ea6aaa3774b0cfd3c3295ff7a1682adeb978e496b3ed0a132a7b1da7d22`.

The PIT readiness report
`manifests/pit_readiness_20260801T053231659856Z_aefe9a41410e.json` independently
rechecks all 900 archive checksums, the manifest self-hash, normalized output
hashes, symbols, dates, and funding continuity. This release removes only the
funding blockers; it is not historical universe or profitability evidence.

## Backfill Gate

With 96.2 GB free, a 2025-01-01 through 2026-06-01 30-symbol backfill is
storage-feasible. Estimated raw depth is 6.9 GB, while OI and three basis inputs
are below 0.5 GB combined; normalized outputs are roughly 1.2 GB. Backfill
OI, basis, and funding first. Limit depth to the eight V3 symbols until a
predeclared matched ablation demonstrates incremental out-of-sample value.

Create every run with a new release ID. Never overwrite a release or join a row
before its `available_at` timestamp. Interrupted runs discard only their hidden,
unpublished staging directory and resume from checksum-verified raw archives.
