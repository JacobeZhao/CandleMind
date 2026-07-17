"""Nested walk-forward audit for sparse cost-aware momentum rules."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.feature_engineering import add_rl_derived_features, build_decision_frame
from backend.scripts.evaluation.audit_rl_decision_timing import load_market_bars
from backend.scripts.evaluation.rl_alpha_baseline import (
    OUTER_WINDOWS,
    forward_open_return,
    hold_comparators,
    moving_block_bootstrap_mean_ci,
    trade_metrics,
)

THRESHOLDS = (1.0, 1.5, 2.0)
HOLD_HOURS = (24, 72, 168)
ENTRY_STRIDES = (1, 4)
TREND_GATES = (False, True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--position-fraction", type=float, default=0.5)
    parser.add_argument("--fee-rate", type=float, default=0.0010)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    bars = add_rl_derived_features(load_market_bars(args.data_root, args.symbol))
    decisions = build_decision_frame(bars, 12)
    decisions["dt"] = _timestamps(decisions["open_time"])
    for horizon in HOLD_HOURS:
        decisions[f"forward_{horizon}h"] = forward_open_return(decisions, horizon)

    report = run_nested_momentum(
        decisions,
        position_fraction=args.position_fraction,
        one_way_cost=args.fee_rate + args.slippage_rate,
    )
    report.update(
        {
            "symbol": args.symbol,
            "position_fraction": args.position_fraction,
            "fee_rate": args.fee_rate,
            "slippage_rate": args.slippage_rate,
            "candidate_count": len(THRESHOLDS) * len(HOLD_HOURS) * len(ENTRY_STRIDES) * len(TREND_GATES),
        }
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def run_nested_momentum(decisions: pd.DataFrame, *, position_fraction: float, one_way_cost: float) -> dict:
    candidates = [
        {
            "threshold": threshold,
            "hold_hours": hold,
            "entry_stride_hours": stride,
            "trend_gate": gate,
        }
        for threshold, hold, stride, gate in itertools.product(
            THRESHOLDS, HOLD_HOURS, ENTRY_STRIDES, TREND_GATES
        )
    ]
    folds = []
    base_returns: list[float] = []
    stress_returns: list[float] = []
    for fold_id, (_, train_end, test_start, test_end) in enumerate(OUTER_WINDOWS, start=1):
        inner_windows = _inner_windows(train_end)
        rankings = []
        for candidate in candidates:
            inner_results = []
            candidate_returns = []
            for inner_start, inner_end in inner_windows:
                frame = _eligible_window(
                    decisions,
                    inner_start,
                    inner_end,
                    horizon_hours=int(candidate["hold_hours"]),
                )
                result = simulate_momentum(
                    frame,
                    candidate,
                    position_fraction=position_fraction,
                    one_way_cost=one_way_cost,
                )
                inner_results.append(result["final_equity"])
                candidate_returns.extend(result["trade_returns"])
            aggregate_equity = float(np.prod(inner_results))
            rankings.append(
                {
                    "candidate": candidate,
                    "inner_fold_equities": inner_results,
                    "inner_final_equity": aggregate_equity,
                    "inner_profitable_folds": int(sum(value > 1.0 for value in inner_results)),
                    "inner_trades": len(candidate_returns),
                    "qualified": bool(
                        aggregate_equity > 1.0
                        and sum(value > 1.0 for value in inner_results) >= 3
                        and len(candidate_returns) >= 40
                    ),
                }
            )
        qualified = [item for item in rankings if item["qualified"]]
        selection_pool = qualified or rankings
        selected = max(selection_pool, key=lambda item: item["inner_final_equity"])
        candidate = selected["candidate"]
        outer = _eligible_window(
            decisions,
            test_start,
            test_end,
            horizon_hours=int(candidate["hold_hours"]),
        )
        base = simulate_momentum(
            outer,
            candidate,
            position_fraction=position_fraction,
            one_way_cost=one_way_cost,
        )
        stress = simulate_momentum(
            outer,
            candidate,
            position_fraction=position_fraction,
            one_way_cost=2.0 * one_way_cost,
        )
        base_returns.extend(base["trade_returns"])
        stress_returns.extend(stress["trade_returns"])
        folds.append(
            {
                "fold": fold_id,
                "test_window": {"start": test_start, "end": test_end},
                "inner_windows": [{"start": start, "end": end} for start, end in inner_windows],
                "qualified_candidates": len(qualified),
                "selected": selected,
                "outer_base": _without_returns(base),
                "outer_2x_cost": _without_returns(stress),
                "comparators": hold_comparators(
                    outer,
                    position_fraction=position_fraction,
                    one_way_cost=one_way_cost,
                ),
            }
        )

    aggregate = _aggregate(folds, base_returns, stress_returns)
    return {
        "search_space": {
            "thresholds": list(THRESHOLDS),
            "hold_hours": list(HOLD_HOURS),
            "entry_stride_hours": list(ENTRY_STRIDES),
            "trend_gates": list(TREND_GATES),
            "selection_rule": "qualified_then_max_inner_final_equity",
        },
        "folds": folds,
        "aggregate": aggregate,
    }


def simulate_momentum(
    frame: pd.DataFrame,
    candidate: dict,
    *,
    position_fraction: float,
    one_way_cost: float,
) -> dict:
    threshold = float(candidate["threshold"])
    horizon = int(candidate["hold_hours"])
    stride = int(candidate["entry_stride_hours"])
    trend_gate = bool(candidate["trend_gate"])
    next_entry = 0
    returns = []
    long_trades = 0
    short_trades = 0
    for index, row in frame.iterrows():
        if index < next_entry or index % stride != 0:
            continue
        score = float(row["1h_ret_6_z"])
        side = 1 if score >= threshold else -1 if score <= -threshold else 0
        if side == 0:
            continue
        if trend_gate and side * float(row["market_trend_score"]) < 2 / 7:
            continue
        gross = side * float(row[f"forward_{horizon}h"])
        returns.append(position_fraction * gross - 2.0 * position_fraction * one_way_cost)
        long_trades += int(side > 0)
        short_trades += int(side < 0)
        next_entry = index + horizon
    return trade_metrics(returns, long_trades=long_trades, short_trades=short_trades)


def _aggregate(folds: list[dict], base_returns: list[float], stress_returns: list[float]) -> dict:
    base_equities = [float(fold["outer_base"]["final_equity"]) for fold in folds]
    stress_equities = [float(fold["outer_2x_cost"]["final_equity"]) for fold in folds]
    base = np.asarray(base_returns, dtype=float)
    lower, upper = moving_block_bootstrap_mean_ci(base, replications=10_000, seed=23)
    metrics = trade_metrics(
        base_returns,
        long_trades=sum(int(fold["outer_base"]["long_trades"]) for fold in folds),
        short_trades=sum(int(fold["outer_base"]["short_trades"]) for fold in folds),
    )
    return {
        "base_final_equity": float(np.prod(base_equities)),
        "base_fold_equities": base_equities,
        "base_profitable_folds": int(sum(value > 1.0 for value in base_equities)),
        "base_mean_trade_return_ci_95": [lower, upper],
        "base_profit_factor": float(metrics["profit_factor"]),
        "base_trades": len(base),
        "cost_2x_final_equity": float(np.prod(stress_equities)),
        "cost_2x_fold_equities": stress_equities,
        "all_inner_selections_qualified": all(fold["selected"]["qualified"] for fold in folds),
        "gate_pass": bool(
            np.prod(base_equities) > 1.0
            and sum(value > 1.0 for value in base_equities) >= 3
            and len(base) >= 100
            and metrics["profit_factor"] >= 1.10
            and lower > 0.0
            and np.prod(stress_equities) > 1.0
            and all(fold["selected"]["qualified"] for fold in folds)
        ),
    }


def _inner_windows(train_end: str) -> list[tuple[str, str]]:
    end = pd.Timestamp(train_end)
    start = end - pd.DateOffset(months=8)
    return [
        (
            str((start + pd.DateOffset(months=2 * index)).date()),
            str((start + pd.DateOffset(months=2 * (index + 1))).date()),
        )
        for index in range(4)
    ]


def _eligible_window(frame: pd.DataFrame, start: str, end: str, *, horizon_hours: int) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    mask = (frame["dt"] >= start_ts) & (
        frame["dt"] + pd.Timedelta(hours=horizon_hours, minutes=5) < end_ts
    )
    return frame.loc[mask].reset_index(drop=True)


def _without_returns(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "trade_returns"}


def _timestamps(series: pd.Series) -> pd.Series:
    if series.dtype.kind == "M":
        return pd.to_datetime(series)
    return pd.to_datetime(series, unit="ms")


if __name__ == "__main__":
    main()
