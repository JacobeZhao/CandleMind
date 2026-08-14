# Backend Commands

Run commands as modules from the repository root. All generated data and reports
must use an explicit path under the external data root.

## Data

```powershell
python -m backend.scripts.data.sync_klines --root G:\CandleMind\CandleMind_data
python -m backend.scripts.data.sync_derivatives --root G:\CandleMind\CandleMind_data --release-id <id> --start <date> --through <date>
python -m backend.scripts.data.build_ema_data_release --help
```

`sync_klines` downloads and validates checksum-backed Binance Vision K-lines.
`sync_derivatives` publishes immutable, causal derivatives releases. The EMA
release builder is retained only to reproduce and verify historical V2 data
releases; it does not train or activate a model.

## SAR+ADX Evaluation

```powershell
python -m backend.scripts.evaluation.run_sar_pyramid_backtest --help
python -m backend.scripts.evaluation.run_backtrader_sar_pyramid --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar_v2 --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar_v3 --help
```

The deterministic and Backtrader runners consume verified OHLCV and observed
funding releases. The sweep commands are research tools for the retained
SOL SAR+ADX strategy. Always use a new output directory and record costs,
funding, time range, and parameters with results.

## Safety

Inspect `--help` before a write-capable command. Never point commands at the
repository, overwrite an immutable release, or treat a parameter sweep as
production evidence.
