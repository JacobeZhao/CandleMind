"""Sweep SOL ADX timeframe/threshold on development data, then test one winner."""

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


SCHEMA = "candlemind-sol-adx-sar-sweep-v1"
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
    parser.add_argument(
        "--timeframes", default="15min,30min,1h,2h,4h",
        help="Comma-separated ADX aggregation intervals.",
    )
    parser.add_argument(
        "--thresholds", default="15,20,25,30,35,40",
        help="Comma-separated ADX thresholds.",
    )
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    manifest = run_sweep(args)
    print(
        json.dumps(
            {
                "output": str(args.output.expanduser().resolve()),
                "selected": manifest["selected"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            indent=2,
        )
    )


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace SOL sweep release: {output}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    if SYMBOL not in funding_manifest["symbols"]:
        raise ValueError("verified funding release does not contain SOLUSDT")

    development_start = _utc(args.development_start)
    development_end = _utc(args.development_end)
    oos_start = _utc(args.oos_start)
    oos_end = _utc(args.oos_end)
    if not development_start < development_end <= oos_start < oos_end:
        raise ValueError("windows must satisfy development_start < end <= oos_start < end")
    timeframes = tuple(dict.fromkeys(item.strip() for item in args.timeframes.split(",") if item.strip()))
    thresholds = tuple(dict.fromkeys(float(item) for item in args.thresholds.split(",") if item.strip()))
    if not timeframes or not thresholds:
        raise ValueError("timeframes and thresholds must be non-empty")

    bars = pd.read_parquet(
        data_root / "ohlcv" / f"{SYMBOL}_5m.parquet",
        columns=["open_time", "open", "high", "low", "close"],
    )
    funding, _ = load_observed_funding_symbol(
        funding_root, SYMBOL, manifest=funding_manifest
    )
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    eligibility = universe.loc[universe["symbol"] == SYMBOL]

    rows: list[dict[str, Any]] = []
    for timeframe in timeframes:
        for threshold in thresholds:
            config = _config(args, timeframe=timeframe, threshold=threshold)
            result = run_sar_pyramid_backtest(
                bars,
                symbol=SYMBOL,
                start=development_start,
                end=development_end,
                funding=funding,
                eligibility=eligibility,
                config=config,
                retain_equity=False,
            )
            rows.append(
                {
                    "adx_timeframe": timeframe,
                    "adx_threshold": threshold,
                    **result.metrics,
                }
            )
    grid = pd.DataFrame(rows).sort_values(
        ["total_return", "max_drawdown", "profit_factor"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)
    selected_row = grid.iloc[0]
    selected_config = _config(
        args,
        timeframe=str(selected_row["adx_timeframe"]),
        threshold=float(selected_row["adx_threshold"]),
    )
    oos = run_sar_pyramid_backtest(
        bars,
        symbol=SYMBOL,
        start=oos_start,
        end=oos_end,
        funding=funding,
        eligibility=eligibility,
        config=selected_config,
        retain_equity=False,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        grid.to_parquet(staging / "development_grid.parquet", index=False, compression="zstd")
        oos.cycles.to_parquet(staging / "selected_oos_cycles.parquet", index=False, compression="zstd")
        oos.fills.to_parquet(staging / "selected_oos_fills.parquet", index=False, compression="zstd")
        summary = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "symbol": SYMBOL,
            "selection_rule": "maximum_development_total_return_then_drawdown_then_profit_factor",
            "development_window": _window(development_start, development_end),
            "oos_window": _window(oos_start, oos_end),
            "search_space": {
                "adx_timeframes": list(timeframes),
                "adx_thresholds": list(thresholds),
                "adx_period": 14,
                "combination_count": len(grid),
            },
            "selected_config": asdict(selected_config),
            "selected_development_metrics": _json_metrics(selected_row.to_dict()),
            "selected_oos_metrics": _json_metrics(oos.metrics),
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
            "development_grid.parquet",
            "selected_oos_cycles.parquet",
            "selected_oos_fills.parquet",
            "summary.json",
        )
        artifacts = {name: _evidence(staging / name) for name in names}
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_manifest["manifest_sha256"],
            "funding_manifest_sha256": funding_manifest["manifest_sha256"],
            "selected": {
                "adx_timeframe": selected_config.adx_timeframe,
                "adx_threshold": selected_config.adx_threshold,
            },
            "artifacts": artifacts,
        }
        manifest["manifest_sha256"] = _canonical_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.rename(staging, output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _config(
    args: argparse.Namespace, *, timeframe: str, threshold: float
) -> SarPyramidConfig:
    config = SarPyramidConfig(
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        use_adx_filter=True,
        adx_timeframe=timeframe,
        adx_period=14,
        adx_threshold=threshold,
    )
    config.validate()
    return config


def _window(start: pd.Timestamp, end: pd.Timestamp) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat(), "semantics": "[start,end)"}


def _json_metrics(values: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value.item() if hasattr(value, "item") else value
        for key, value in values.items()
    }


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
