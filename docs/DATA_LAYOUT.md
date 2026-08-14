# Data And Artifact Layout

## Ownership

The repository contains application source, tests, configuration templates,
documentation, and supported commands. Market data, reports, logs, databases,
secrets, caches, frontend builds, and model artifacts remain outside Git.

The Windows market-data root defaults to
`G:/CandleMind/CandleMind_data` and can be overridden with
`MARKET_DATA_DIR`. `DATA_DIR` selects runtime database/key storage.

## External Layout

```text
CandleMind_data/
|-- raw/
|   |-- klines_archive/
|   |-- funding/
|   `-- derivatives_archive/
|-- normalized/
|   |-- ohlcv_parquet/
|   |-- ema/releases/
|   `-- derivatives/releases/
|-- processed/
|   `-- features_app/
|-- experiments/
|   |-- backtests/
|   `-- reports/
|-- runtime/
|   `-- app/
`-- manifests/
```

`backend/app/datastore.py` owns the authoritative market-data root. The
repository must not contain a fallback `data/` tree. Market-data directories
must already exist and pass `backend/app/data_layout.py` validation; importing
the application does not create directories under that root.

`DATA_DIR` owns application state under `runtime/app`, including the database,
encryption key, and `strategies/sar_adx_paper_<symbol>.json` restart state.

## Release Rules

K-line and derivatives commands publish validated outputs atomically. Never
modify an immutable release in place. SAR+ADX backtests must bind their OHLCV,
funding, parameters, costs, and code revision. Historical artifacts may remain
in external archives, but no model directory is required by the current runtime.

Before moving or deleting external data, inventory paths and checksums, verify
containment, preserve raw data and runtime key/database pairs, and keep the
newest verified backup.
