# CandleMind G-Drive Data Store

This directory is the external data and artifact store for CandleMind. The
application data root is:

```text
G:\CandleMind\CandleMind_data
```

Set `MARKET_DATA_DIR` only when an intentional override is required. The
directory constants in `backend/app/datastore.py` define the authoritative
application paths.

## Directory Map

```text
G:\CandleMind\
|-- CandleMind_data\
|   |-- raw\
|   |   |-- klines_json\
|   |   `-- funding\
|   |-- normalized\
|   |   `-- ohlcv_parquet\
|   |-- processed\
|   |   |-- features_app\
|   |   |-- features_ml\
|   |   `-- labels\
|   |-- models\
|   |   |-- current\
|   |   |   `-- ACTIVE
|   |   |-- releases\
|   |   |-- candidates\
|   |   |   `-- supervised\
|   |   |-- archive\
|   |   `-- rl\
|   |       `-- candidates\
|   |-- experiments\
|   |   |-- backtests\
|   |   |-- reports\
|   |   `-- experiments.db
|   |-- runtime\
|   |   |-- app\
|   |   |-- journal\
|   |   `-- regime_cache\
|   `-- manifests\
`-- CandleMind_backups\
```

`CandleMind_backups` is a sibling of `CandleMind_data`, not a child of it.
Timestamped snapshots belong under `CandleMind_backups`; do not mix backups
with live data or model directories.

`runtime/app` contains `trader.db` and its matching `secret.key`. Never replace
one without the other. Host execution defaults to this directory on Windows;
Docker mounts it at `/app/runtime` and uses `DATA_DIR=/app/runtime`.

## Current Supervised Release

The current supervised release is the immutable directory under
`models/releases` named by `models/current/ACTIVE`. An older layout may omit
that pointer only while `models/current` contains exactly one release directory.

Treat this release as immutable. Do not add, overwrite, or retrain files in
place. Build a complete candidate in a new directory, verify its data lineage,
metrics, metadata, and SHA-256 hashes, seal `release_manifest.json`, then use
`python -m backend.scripts.artifacts.promote_supervised_release` to move and
activate the whole release.

## RL Candidates

RL artifacts remain under `models/rl/candidates`. They are research candidates,
not production models. Each run directory keeps its own `manifest.json` and
related training/evaluation metadata. A candidate stays here until it passes
the required walk-forward, cost, and cross-symbol stress gates; failed or
rejected candidates must not be copied into `models/current`.

## Manifests

`manifests/inventory_current.json` is the current machine-readable inventory of
the data root. Regenerate it after an intentional data or release change.
`manifests/INVENTORY.md` is a migration-time record retained for history; it is
not authoritative for current paths or model selection. RL run manifests stay
beside their artifacts under `models/rl/candidates/.../manifest.json`.

Before moving or deleting data, create or refresh an inventory with hashes and
verify that a recoverable snapshot exists under the sibling backup directory.
