"""Bound SOL SAR re-entry frequency within each one-hour ADX trend regime."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence
import uuid

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.strategies.sar_pyramid import SarPyramidConfig, run_sar_pyramid_backtest  # noqa: E402
from backend.app.services.market_release import verify_market_release  # noqa: E402
from backend.app.services.funding_release import (  # noqa: E402
    load_observed_funding_symbol,
    verify_observed_funding_release,
)


SCHEMA = "candlemind-sol-adx-sar-regime-entry-sweep-v3"
SYMBOL = "SOLUSDT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    manifest = run_sweep(args)
    print(json.dumps({"output": str(args.output.resolve()), "selected": manifest["selected"], "manifest_sha256": manifest["manifest_sha256"]}, indent=2))


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    destination = args.output.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to replace SOL V3 sweep: {destination}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    bars = pd.read_parquet(
        data_root / "ohlcv" / f"{SYMBOL}_5m.parquet",
        columns=["open_time", "open", "high", "low", "close"],
    )
    funding, _ = load_observed_funding_symbol(funding_root, SYMBOL, manifest=funding_manifest)
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    eligibility = universe.loc[universe["symbol"] == SYMBOL]
    development_start, development_end = _utc("2024-01-01"), _utc("2026-01-01")
    reused_start, reused_end = _utc("2026-01-01"), _utc("2026-07-01")

    rows: list[dict[str, Any]] = []
    for threshold in (35.0, 40.0, 45.0, 50.0):
        for confirmation in (3, 6, 12):
            for entry_cap in (1, 2):
                config = _config(threshold, confirmation, entry_cap)
                result = _run(bars, funding, eligibility, development_start, development_end, config)
                rows.append({**_parameters(config), **result.metrics})
    grid = pd.DataFrame(rows)
    eligible_grid = grid.loc[grid["cycle_count"] >= 100].copy()
    if eligible_grid.empty:
        raise ValueError("no V3 candidate has at least 100 development cycles")
    grid = eligible_grid.sort_values(
        ["total_return", "max_drawdown", "profit_factor"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)
    winner = grid.iloc[0]
    selected = _config(
        float(winner["adx_threshold"]),
        int(winner["entry_confirmation_bars"]),
        int(winner["max_entries_per_adx_regime"]),
    )
    reused = _run(bars, funding, eligibility, reused_start, reused_end, selected)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        grid.to_parquet(staging / "development_grid.parquet", index=False, compression="zstd")
        reused.cycles.to_parquet(staging / "reused_2026h1_cycles.parquet", index=False, compression="zstd")
        reused.fills.to_parquet(staging / "reused_2026h1_fills.parquet", index=False, compression="zstd")
        summary = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "symbol": SYMBOL,
            "fixed_skeleton": {
                "sar_timeframe": "5min",
                "adx_timeframe": "1h",
                "adx_period": 14,
                "layers": 5,
                "layer_fraction": 0.2,
            },
            "selection_window": _window(development_start, development_end),
            "reused_diagnostic_window": _window(reused_start, reused_end),
            "reused_window_is_not_oos": True,
            "selected_config": asdict(selected),
            "selected_development_metrics": _json_values(winner.to_dict()),
            "reused_2026h1_metrics": _json_values(reused.metrics),
            "admission": {"promotable": False, "reason": "2026H1 has already been inspected"},
        }
        _write_json(staging / "summary.json", summary)
        names = (
            "development_grid.parquet", "reused_2026h1_cycles.parquet",
            "reused_2026h1_fills.parquet", "summary.json",
        )
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_manifest["manifest_sha256"],
            "funding_manifest_sha256": funding_manifest["manifest_sha256"],
            "selected": _parameters(selected),
            "artifacts": {name: _evidence(staging / name) for name in names},
        }
        manifest["manifest_sha256"] = _canonical_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.rename(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _config(threshold: float, confirmation: int, entry_cap: int) -> SarPyramidConfig:
    config = SarPyramidConfig(
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_rate=0.0002,
        use_adx_filter=True,
        adx_timeframe="1h",
        adx_period=14,
        adx_threshold=threshold,
        adx_rising_periods=2,
        entry_confirmation_bars=confirmation,
        recapture_buffer_fraction=0.0024,
        require_progressive_adds=True,
        max_entries_per_adx_regime=entry_cap,
    )
    config.validate()
    return config


def _run(bars, funding, eligibility, start, end, config):
    return run_sar_pyramid_backtest(
        bars, symbol=SYMBOL, start=start, end=end, funding=funding,
        eligibility=eligibility, config=config, retain_equity=False,
    )


def _parameters(config: SarPyramidConfig) -> dict[str, Any]:
    return {
        "adx_threshold": config.adx_threshold,
        "entry_confirmation_bars": config.entry_confirmation_bars,
        "max_entries_per_adx_regime": config.max_entries_per_adx_regime,
    }


def _window(start, end):
    return {"start": start.isoformat(), "end": end.isoformat(), "semantics": "[start,end)"}


def _json_values(values):
    return {str(key): value.item() if hasattr(value, "item") else value for key, value in values.items()}


def _evidence(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


if __name__ == "__main__":
    main()
