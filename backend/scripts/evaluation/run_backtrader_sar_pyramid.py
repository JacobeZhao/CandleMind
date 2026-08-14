"""Run the frozen SOL SAR/ADX strategy through Backtrader's broker engine."""

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

import backtrader as bt
import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.strategies.sar_pyramid import SarPyramidConfig  # noqa: E402
from backend.app.strategies.sar_pyramid_backtrader import (  # noqa: E402
    prepare_backtrader_signal_frame,
    run_backtrader_sar_pyramid,
)
from backend.app.services.funding_release import (  # noqa: E402
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from backend.app.services.market_release import verify_market_release  # noqa: E402


SCHEMA = "candlemind-backtrader-sar-pyramid-release-v1"
SYMBOL = "SOLUSDT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--adx-threshold", type=float, default=45.0)
    parser.add_argument("--entry-confirmation-bars", type=int, default=6)
    parser.add_argument("--max-entries-per-adx-regime", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    manifest = run_release(args)
    print(json.dumps({"output": str(args.output.resolve()), "manifest_sha256": manifest["manifest_sha256"]}, indent=2))


def run_release(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    destination = args.output.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to replace Backtrader release: {destination}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    start, end = _utc(args.start), _utc(args.end)
    if end <= start:
        raise ValueError("end must be after start")
    config = SarPyramidConfig(
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_rate=0.0002,
        use_adx_filter=True,
        adx_timeframe="1h",
        adx_period=14,
        adx_threshold=args.adx_threshold,
        adx_rising_periods=2,
        entry_confirmation_bars=args.entry_confirmation_bars,
        recapture_buffer_fraction=0.0024,
        require_progressive_adds=True,
        max_entries_per_adx_regime=args.max_entries_per_adx_regime,
    )
    config.validate()
    bars = pd.read_parquet(
        data_root / "ohlcv" / f"{SYMBOL}_5m.parquet",
        columns=["open_time", "open", "high", "low", "close"],
    )
    funding, _ = load_observed_funding_symbol(
        funding_root, SYMBOL, manifest=funding_manifest
    )
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    eligibility = universe.loc[universe["symbol"] == SYMBOL]
    tape = prepare_backtrader_signal_frame(
        bars,
        funding=funding,
        eligibility=eligibility,
        start=start,
        end=end,
        config=config,
    )
    result = run_backtrader_sar_pyramid(tape, config=config)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        result.fills.to_parquet(staging / "fills.parquet", index=False, compression="zstd")
        result.trades.to_parquet(staging / "trades.parquet", index=False, compression="zstd")
        result.funding.to_parquet(staging / "funding.parquet", index=False, compression="zstd")
        result.equity.to_parquet(staging / "equity.parquet", index=False, compression="zstd")
        summary = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "engine": {"name": "backtrader", "version": bt.__version__},
            "symbol": SYMBOL,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "semantics": "[start,end)"},
            "config": asdict(config),
            "metrics": result.metrics,
            "funding_semantics": "cashflow_at_event_open_before_same_open_orders",
        }
        _write_json(staging / "summary.json", summary)
        names = ("fills.parquet", "trades.parquet", "funding.parquet", "equity.parquet", "summary.json")
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_manifest["manifest_sha256"],
            "funding_manifest_sha256": funding_manifest["manifest_sha256"],
            "artifacts": {name: _evidence(staging / name) for name in names},
        }
        manifest["manifest_sha256"] = _canonical_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.rename(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
