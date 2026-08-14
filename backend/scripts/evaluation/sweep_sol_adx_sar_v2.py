"""Optimize bounded entry and add gates within the fixed SOL SAR/ADX skeleton."""

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

from backend.app.strategies.sar_pyramid import (  # noqa: E402
    SarPyramidConfig,
    run_sar_pyramid_backtest,
)
from backend.app.services.funding_release import (  # noqa: E402
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from backend.app.services.market_release import verify_market_release  # noqa: E402


SCHEMA = "candlemind-sol-adx-sar-structural-sweep-v2"
SYMBOL = "SOLUSDT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--development-start", default="2024-01-01")
    parser.add_argument("--development-end", default="2026-01-01")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--oos-end", default="2026-07-01")
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
        raise FileExistsError(f"refusing to replace SOL V2 sweep: {destination}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    development_start = _utc(args.development_start)
    development_end = _utc(args.development_end)
    oos_start = _utc(args.oos_start)
    oos_end = _utc(args.oos_end)
    if not development_start < development_end <= oos_start < oos_end:
        raise ValueError("invalid development/OOS windows")

    bars = pd.read_parquet(
        data_root / "ohlcv" / f"{SYMBOL}_5m.parquet",
        columns=["open_time", "open", "high", "low", "close"],
    )
    funding, _ = load_observed_funding_symbol(
        funding_root, SYMBOL, manifest=funding_manifest
    )
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    eligibility = universe.loc[universe["symbol"] == SYMBOL]

    entry_rows: list[dict[str, Any]] = []
    for rising in (0, 1, 2):
        for confirmation in (1, 3, 6):
            for di_spread in (0.0, 5.0):
                config = _config(
                    rising=rising,
                    confirmation=confirmation,
                    di_spread=di_spread,
                    recapture_buffer=0.0024,
                )
                metrics = _run(
                    bars, funding, eligibility, development_start, development_end, config
                ).metrics
                entry_rows.append({**_parameters(config), **metrics})
    entry_grid = _rank(pd.DataFrame(entry_rows), minimum_cycles=100)
    entry_winner = entry_grid.iloc[0]

    buffer_rows: list[dict[str, Any]] = []
    for buffer in (0.0, 0.0012, 0.0024, 0.005):
        config = _config(
            rising=int(entry_winner["adx_rising_periods"]),
            confirmation=int(entry_winner["entry_confirmation_bars"]),
            di_spread=float(entry_winner["minimum_di_spread"]),
            recapture_buffer=buffer,
        )
        metrics = _run(
            bars, funding, eligibility, development_start, development_end, config
        ).metrics
        buffer_rows.append({**_parameters(config), **metrics})
    buffer_grid = _rank(pd.DataFrame(buffer_rows), minimum_cycles=100)
    selected_row = buffer_grid.iloc[0]
    selected = _config(
        rising=int(selected_row["adx_rising_periods"]),
        confirmation=int(selected_row["entry_confirmation_bars"]),
        di_spread=float(selected_row["minimum_di_spread"]),
        recapture_buffer=float(selected_row["recapture_buffer_fraction"]),
    )
    oos = _run(bars, funding, eligibility, oos_start, oos_end, selected)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        entry_grid.to_parquet(staging / "entry_grid.parquet", index=False, compression="zstd")
        buffer_grid.to_parquet(staging / "buffer_grid.parquet", index=False, compression="zstd")
        oos.cycles.to_parquet(staging / "selected_oos_cycles.parquet", index=False, compression="zstd")
        oos.fills.to_parquet(staging / "selected_oos_fills.parquet", index=False, compression="zstd")
        summary = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "symbol": SYMBOL,
            "fixed_skeleton": {
                "sar_timeframe": "5min",
                "adx_timeframe": "1h",
                "adx_period": 14,
                "adx_threshold": 40.0,
                "layers": 5,
                "layer_fraction": 0.2,
            },
            "selection_rule": "maximum_development_return_with_at_least_100_cycles",
            "development_window": _window(development_start, development_end),
            "oos_window": _window(oos_start, oos_end),
            "selected_config": asdict(selected),
            "selected_development_metrics": _json_values(selected_row.to_dict()),
            "selected_oos_metrics": _json_values(oos.metrics),
            "admission": {
                "profitable_after_costs": bool(oos.metrics["total_return"] > 0.0),
                "profit_factor_above_one": bool(
                    oos.metrics["profit_factor"] is not None
                    and oos.metrics["profit_factor"] > 1.0
                ),
                "promotable": False,
            },
        }
        _write_json(staging / "summary.json", summary)
        names = (
            "entry_grid.parquet", "buffer_grid.parquet", "selected_oos_cycles.parquet",
            "selected_oos_fills.parquet", "summary.json",
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


def _config(
    *, rising: int, confirmation: int, di_spread: float, recapture_buffer: float
) -> SarPyramidConfig:
    config = SarPyramidConfig(
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_rate=0.0002,
        use_adx_filter=True,
        adx_timeframe="1h",
        adx_period=14,
        adx_threshold=40.0,
        adx_rising_periods=rising,
        minimum_di_spread=di_spread,
        entry_confirmation_bars=confirmation,
        recapture_buffer_fraction=recapture_buffer,
        require_progressive_adds=True,
    )
    config.validate()
    return config


def _run(
    bars: pd.DataFrame,
    funding: pd.DataFrame,
    eligibility: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    config: SarPyramidConfig,
):
    return run_sar_pyramid_backtest(
        bars,
        symbol=SYMBOL,
        start=start,
        end=end,
        funding=funding,
        eligibility=eligibility,
        config=config,
        retain_equity=False,
    )


def _parameters(config: SarPyramidConfig) -> dict[str, Any]:
    return {
        "adx_rising_periods": config.adx_rising_periods,
        "entry_confirmation_bars": config.entry_confirmation_bars,
        "minimum_di_spread": config.minimum_di_spread,
        "recapture_buffer_fraction": config.recapture_buffer_fraction,
    }


def _rank(frame: pd.DataFrame, *, minimum_cycles: int) -> pd.DataFrame:
    eligible = frame.loc[frame["cycle_count"] >= minimum_cycles].copy()
    if eligible.empty:
        raise ValueError("no parameter combination meets minimum cycle coverage")
    return eligible.sort_values(
        ["total_return", "max_drawdown", "profit_factor"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)


def _window(start: pd.Timestamp, end: pd.Timestamp) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat(), "semantics": "[start,end)"}


def _json_values(values: dict[str, Any]) -> dict[str, Any]:
    return {str(key): value.item() if hasattr(value, "item") else value for key, value in values.items()}


def _evidence(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def _utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


if __name__ == "__main__":
    main()
