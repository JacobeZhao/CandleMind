"""Test staged risk management on the fixed multi-symbol SAR entry candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Sequence
import uuid

import pandas as pd

from backend.app.services.funding_release import (
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from backend.app.services.market_release import verify_market_release
from backend.scripts.evaluation.sweep_multi_symbol_sar_market_gate import (
    GATES,
    SYMBOLS,
    WINDOWS,
    _admission,
    _bars_with_time,
    _canonical_hash,
    _candidate_summary,
    _evidence,
    _json_rows,
    _metric_record,
    _ms,
    _portfolio_metrics,
    _prepare_symbol,
    _simulate_window,
    _write_json,
    completed_btc_regime,
)


SCHEMA = "candlemind-multi-symbol-sar-staged-risk-diagnostic-v1"
ARCHITECTURES = (
    ("baseline", 12, 0),
    ("fast_initial_exit", 1, 0),
    ("staged_exit_1", 1, 1),
    ("staged_exit_3", 3, 1),
    ("staged_exit_6", 6, 1),
)
GATE_CODES = (0, 3, 5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    print(json.dumps(run_diagnostic(args), indent=2))


def run_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace diagnostic: {output}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    btc = _bars_with_time(pd.read_parquet(data_root / "ohlcv" / "BTCUSDT_5m.parquet"))
    regime = completed_btc_regime(btc)
    market = pd.DataFrame({"open_time": btc["open_time"], **regime.to_dict("series")})
    prepared = {}
    for symbol in SYMBOLS:
        bars = pd.read_parquet(data_root / "ohlcv" / f"{symbol}_5m.parquet")
        funding, _ = load_observed_funding_symbol(funding_root, symbol, manifest=funding_manifest)
        prepared[symbol] = _prepare_symbol(bars, funding, universe, market, symbol)

    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for architecture, exit_bars, confirmation_add in ARCHITECTURES:
        for gate_code in GATE_CODES:
            for window, start, end in WINDOWS:
                symbol_metrics = []
                for symbol in SYMBOLS:
                    values = _simulate_window(
                        prepared[symbol], _ms(start), _ms(end), 24, 1.2,
                        gate_code, 0, exit_bars, confirmation_add,
                    )
                    metrics = _metric_record(values)
                    symbol_metrics.append(metrics)
                    details.append({
                        "entry_style": architecture,
                        "lookback": 24,
                        "atr_expansion": 1.2,
                        "market_gate": GATES[gate_code],
                        "window": window,
                        "symbol": symbol,
                        **metrics,
                    })
                rows.append({
                    "entry_style": architecture,
                    "lookback": 24,
                    "atr_expansion": 1.2,
                    "market_gate": GATES[gate_code],
                    "window": window,
                    **_portfolio_metrics(symbol_metrics),
                })
    grid = pd.DataFrame(rows)
    detail = pd.DataFrame(details)
    candidates = _candidate_summary(grid, detail)
    decision = _admission(candidates)
    _publish(
        output, grid, detail, candidates, decision,
        data_manifest["manifest_sha256"], funding_manifest["manifest_sha256"],
    )
    return {"output": str(output), "decision": decision, "best": _json_rows(candidates.head(5))}


def _publish(
    output: Path,
    grid: pd.DataFrame,
    detail: pd.DataFrame,
    candidates: pd.DataFrame,
    decision: dict[str, Any],
    data_hash: str,
    funding_hash: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        grid.to_parquet(staging / "window_grid.parquet", index=False, compression="zstd")
        detail.to_parquet(staging / "symbol_detail.parquet", index=False, compression="zstd")
        candidates.to_parquet(staging / "candidate_summary.parquet", index=False, compression="zstd")
        report = {
            "schema": SCHEMA,
            "status": "diagnostic_only",
            "historical_periods_previously_inspected": True,
            "fixed_entry": "breakout lookback=24, ATR expansion=1.2, three-bar confirmation",
            "architectures": {
                "baseline": "12 opposite SAR bars; recapture adds only",
                "fast_initial_exit": "one opposite SAR bar before every first add",
                "staged_exit_N": "N opposite SAR bars before confirmation; 12 after confirmation",
                "confirmation_add": "second 20% layer when ADX>=25, rising over 1h, DI agrees, and price progressed",
            },
            "requirements": {"minimum_annual_trades": 600, "all_windows_profitable": True},
            "decision": decision,
            "best_candidates": _json_rows(candidates.head(8)),
        }
        _write_json(staging / "report.json", report)
        names = ("window_grid.parquet", "symbol_detail.parquet", "candidate_summary.parquet", "report.json")
        manifest = {
            "schema": SCHEMA,
            "status": "completed",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_hash,
            "funding_manifest_sha256": funding_hash,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "shared_runner_sha256": hashlib.sha256(
                Path(_simulate_window.py_func.__code__.co_filename).read_bytes()
            ).hexdigest(),
            "artifacts": {name: _evidence(staging / name) for name in names},
        }
        manifest["manifest_sha256"] = _canonical_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.rename(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == "__main__":
    main()
