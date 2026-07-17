# Backend Script Catalog

Run commands as modules from the repository root. Generated data, models, and
reports must stay under the external data root.

```powershell
python -m backend.scripts.artifacts.inventory_data_root --help
```

## Directory Ownership

- `data/`: deterministic market-data and label preparation.
- `training/`: supervised and RL candidate training orchestration.
- `evaluation/`: audits, baselines, backtests, diagnostics, and stress tests.
- `artifacts/`: inventory, manifests, promotion, registries, snapshots, and
  destructive cleanup utilities.

Scripts are entry points, not general-purpose libraries. Move reusable domain
logic into `backend/app/` before sharing it between commands.

## Supported Workflows

Create supervised labels with `data.build_trend_labels` or
`data.build_multi_horizon_labels`. Train a versioned candidate with
`training.retrain_multi_horizon --release-id <id>`. Then use these artifact
commands in order:

1. `artifacts.create_model_release_manifest`
2. `artifacts.promote_supervised_release`
3. `artifacts.inventory_data_root`

Promoted files under `models/releases/` are immutable. No training command may
write to `models/current/` or `models/releases/`.

RL commands remain research-only. Use `training.rl_train` and
`training.rl_walk_forward` for candidates, then evaluation commands for causal,
cost, timing, seed, and cross-symbol checks. No RL command promotes a policy to
production.

## Safety

Inspect `--help` before every write-capable command. `artifacts.gdisk_cleanup`,
`artifacts.reject_rl_candidates`, registry rebuilds, snapshots, and promotion
require an inventory and a verified backup. `artifacts.publish_deployment_cache`
publishes only to an explicit external cache and is not a model promotion tool.
