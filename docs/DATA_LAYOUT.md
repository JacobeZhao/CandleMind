# Data and Artifact Layout

## Ownership Boundary

The Git repository owns reproducible inputs to development: application source,
tests, configuration templates, documentation, container definitions, and
maintenance scripts. Generated market data, trained models, reports, logs,
databases, credentials, caches, virtual environments, and frontend builds stay
outside the repository.

## G-Drive Layout

```text
G:\CandleMind\
|-- CandleMind_data\
|   |-- raw\
|   |   |-- klines_json\      # Immutable source K-line downloads
|   |   `-- funding\          # Funding-rate source data
|   |-- normalized\
|   |   `-- ohlcv_parquet\    # Canonical OHLCV tables
|   |-- processed\
|   |   |-- features_app\     # Lightweight application features
|   |   |-- features_ml\      # ML training feature matrices
|   |   `-- labels\           # Supervised labels by variant
|   |-- models\
|   |   |-- current\
|   |   |   `-- ACTIVE                 # Active supervised release ID
|   |   |-- releases\                  # Immutable promoted releases
|   |   |-- candidates\supervised\    # Unpromoted supervised releases
|   |   |-- archive\          # Superseded immutable releases
|   |   `-- rl\
|   |       `-- candidates\   # Unpromoted RL run directories
|   |-- experiments\
|   |   |-- backtests\
|   |   |-- reports\
|   |   `-- experiments.db
|   |-- runtime\
|   |   |-- app\             # trader.db and matching secret.key
|   |   |-- journal\
|   |   `-- regime_cache\
|   `-- manifests\           # Root inventory and migration records
`-- CandleMind_backups\      # Timestamped snapshots; never nested in data
```

The application resolves this root through `MARKET_DATA_DIR`, defaulting to
`G:/CandleMind/CandleMind_data` on Windows. `DATA_DIR` selects the paired
application database/key directory and defaults to `runtime/app` on Windows.
Docker bind-mounts both external locations; it does not create repository data.

The directory constants in `backend/app/datastore.py` are authoritative. Do
not reconstruct clean-layout paths by appending old flat subdirectory names to
`MARKET_ROOT`; import `KLINES_DIR`, `FUNDING_DIR`, `PARQUET_DIR`,
`FEATURES_DIR`, `FEATURES_ML_DIR`, `LABELS_DIR`, `BACKTEST_DIR`,
`REPORTS_DIR`, `JOURNAL_DIR`, or `REGIME_DIR` as appropriate.

The active supervised release is selected by `models/current/ACTIVE`, or by the
sole release directory while migrating an older layout. RL artifacts are candidates,
not production models, and remain under `models/rl/candidates` with a
`manifest.json` in each run directory.

`manifests/inventory_current.json` is the current root inventory. The existing
`manifests/INVENTORY.md` is a migration-time record and must not be used as the
source of current paths or release selection.

## Model Release Rules

Never append, replace, or retrain files in an already promoted release. Train
into a new candidate directory, record the data snapshot, feature set, label
variant, time windows, cost assumptions, code revision, metrics, and SHA-256
hashes, then promote the whole directory as a new release. Move superseded
releases to `models/archive`; `models/current` must identify exactly one
supervised release. RL models remain under versioned candidate directories
until their walk-forward and stress gates pass.

## Cleanup Rules

1. Generate a current inventory and checksums before moving or deleting data.
2. Validate source and destination containment for every destructive operation.
3. Preserve raw data, active models, runtime key/database pairs, and the newest
   verified backup.
4. Treat `__pycache__/`, `.pytest_cache/`, `frontend/dist/`, dependency
   directories, and local model copies as rebuildable.
5. Do not consolidate overlapping raw snapshots by filename alone; compare
   time coverage and content first.
