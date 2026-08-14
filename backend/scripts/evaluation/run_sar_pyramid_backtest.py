"""Run and publish the strict 5-minute SAR pyramiding diagnostic baseline."""

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


SCHEMA = "candlemind-sar-pyramid-diagnostic-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbol", action="append", dest="symbols", type=str.upper)
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--target-notional-fraction", type=float, default=1.0)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    parser.add_argument("--adx-filter", action="store_true")
    parser.add_argument("--adx-timeframe", default="1h")
    parser.add_argument("--adx-period", type=int, default=14)
    parser.add_argument("--adx-threshold", type=float, default=25.0)
    parser.add_argument("--adx-rising-periods", type=int, default=0)
    parser.add_argument("--minimum-di-spread", type=float, default=0.0)
    parser.add_argument("--entry-confirmation-bars", type=int, default=0)
    parser.add_argument("--recapture-buffer-fraction", type=float, default=0.0)
    parser.add_argument("--require-progressive-adds", action="store_true")
    parser.add_argument("--max-entries-per-adx-regime", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    manifest = run_release(args)
    print(json.dumps({"output": str(args.output.resolve()), "manifest_sha256": manifest["manifest_sha256"]}, indent=2))


def run_release(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace SAR diagnostic release: {output}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    available = sorted(item["symbol"] for item in data_manifest["outputs"] if item.get("kind") == "ohlcv")
    symbols = sorted(set(args.symbols or available))
    if not symbols or not set(symbols).issubset(available):
        raise ValueError("requested symbols are not a subset of the verified OHLC release")
    if not set(symbols).issubset(funding_manifest["symbols"]):
        raise ValueError("requested symbols are absent from the funding release")
    start = _utc(args.start)
    end = _utc(args.end)
    if end <= start:
        raise ValueError("end must be after start")
    config = SarPyramidConfig(
        initial_cash=args.initial_cash / len(symbols),
        target_notional_fraction=args.target_notional_fraction,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        use_adx_filter=args.adx_filter,
        adx_timeframe=args.adx_timeframe,
        adx_period=args.adx_period,
        adx_threshold=args.adx_threshold,
        adx_rising_periods=args.adx_rising_periods,
        minimum_di_spread=args.minimum_di_spread,
        entry_confirmation_bars=args.entry_confirmation_bars,
        recapture_buffer_fraction=args.recapture_buffer_fraction,
        require_progressive_adds=args.require_progressive_adds,
        max_entries_per_adx_regime=args.max_entries_per_adx_regime,
    )
    config.validate()
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")

    results = []
    for symbol in symbols:
        bars = pd.read_parquet(data_root / "ohlcv" / f"{symbol}_5m.parquet", columns=["open_time", "open", "high", "low", "close"])
        funding, _ = load_observed_funding_symbol(funding_root, symbol, manifest=funding_manifest)
        results.append(
            run_sar_pyramid_backtest(
                bars,
                symbol=symbol,
                start=start,
                end=end,
                funding=funding,
                eligibility=universe.loc[universe["symbol"] == symbol],
                config=config,
                retain_equity=False,
            )
        )

    destination_parent = output.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    staging = destination_parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        cycles = pd.concat([result.cycles for result in results], ignore_index=True)
        fills = pd.concat([result.fills for result in results], ignore_index=True)
        per_symbol = pd.DataFrame([{"symbol": result.symbol, **result.metrics} for result in results])
        aggregate = _aggregate_metrics(per_symbol, cycles)
        summary = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "window": {"start": start.isoformat(), "end": end.isoformat(), "semantics": "[start,end)"},
            "portfolio_semantics": "equal_weight_fixed_symbol_subaccounts_with_monthly_pit_eligibility",
            "symbols": symbols,
            "config_per_symbol": asdict(config),
            "aggregate_metrics": aggregate,
        }
        _write_json(staging / "summary.json", summary)
        cycles.to_parquet(staging / "cycles.parquet", index=False, compression="zstd")
        fills.to_parquet(staging / "fills.parquet", index=False, compression="zstd")
        per_symbol.to_parquet(staging / "per_symbol.parquet", index=False, compression="zstd")
        artifacts = {name: _evidence(staging / name) for name in ("summary.json", "cycles.parquet", "fills.parquet", "per_symbol.parquet")}
        release_manifest = {
            "schema": SCHEMA,
            "status": "diagnostic",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_manifest["manifest_sha256"],
            "funding_manifest_sha256": funding_manifest["manifest_sha256"],
            "artifacts": artifacts,
        }
        release_manifest["manifest_sha256"] = _canonical_hash(release_manifest)
        _write_json(staging / "manifest.json", release_manifest)
        os.rename(staging, output)
        return release_manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _aggregate_metrics(per_symbol: pd.DataFrame, cycles: pd.DataFrame) -> dict[str, Any]:
    initial = float(per_symbol["initial_cash"].sum())
    final = float(per_symbol["final_equity"].sum())
    pnl = cycles["net_pnl"] if not cycles.empty else pd.Series(dtype=float)
    losses = pnl[pnl < 0]
    wins = pnl[pnl > 0]
    return {
        "initial_cash": initial,
        "final_equity": final,
        "total_return": final / initial - 1.0,
        "cycle_count": int(len(cycles)),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "net_pnl": float(pnl.sum()),
        "fees": float(per_symbol["fees"].sum()),
        "funding_pnl": float(per_symbol["funding_pnl"].sum()),
        "turnover": float(per_symbol["turnover"].sum()),
        "add_count": int(per_symbol["add_count"].sum()),
        "median_symbol_return": float(per_symbol["total_return"].median()),
        "profitable_symbol_count": int((per_symbol["total_return"] > 0).sum()),
        "worst_symbol_max_drawdown": float(per_symbol["max_drawdown"].min()),
        "exhausted_symbol_count": int(per_symbol["account_exhausted"].sum()),
    }


def _evidence(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def _utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


if __name__ == "__main__":
    main()
