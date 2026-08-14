# CandleMind G-Drive Store

The authoritative data root is:

```text
G:\CandleMind\CandleMind_data
```

Keep backups as a sibling of live data, never inside it:

```text
G:\CandleMind\
|-- CandleMind_data\
`-- CandleMind_backups\
```

Required live-data groups are `raw`, `normalized`, `processed`,
`experiments`, `runtime`, and `manifests`. Current application reads use
`normalized/ohlcv_parquet`, `normalized/ema/releases`, and
`normalized/derivatives/releases`. Paper runtime database, encryption key, and
`strategies/sar_adx_paper_<symbol>.json` restart state belong together under
`runtime/app`; SAR+ADX backtest outputs belong under `experiments/backtests` or
`experiments/reports`.

Use the supported commands in `backend/scripts/README.md`. Do not hand-edit
immutable release files, mix generated data into the Git repository, or delete
raw data based only on matching filenames. Historical artifacts may remain
archived on G drive but are not part of the current application.
