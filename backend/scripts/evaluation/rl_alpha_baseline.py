"""Preregistered non-RL alpha baselines on the corrected decision ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.feature_engineering import (
    FEATURE_SETS,
    FEATURE_SET_MARKET_V2,
    SCALED_BASE_COLUMNS,
    add_rl_derived_features,
    apply_feature_scaler,
    build_decision_frame,
    fit_feature_scaler,
)
from backend.scripts.evaluation.audit_rl_decision_timing import load_market_bars


OUTER_WINDOWS = (
    ("2023-01-01", "2025-01-01", "2025-01-01", "2025-04-01"),
    ("2023-04-01", "2025-04-01", "2025-04-01", "2025-07-01"),
    ("2023-07-01", "2025-07-01", "2025-07-01", "2025-10-01"),
    ("2023-10-01", "2025-10-01", "2025-10-01", "2026-01-01"),
    ("2024-01-01", "2026-01-01", "2026-01-01", "2026-04-01"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--decision-interval-bars", type=int, default=12)
    parser.add_argument("--position-fraction", type=float, default=0.5)
    parser.add_argument("--fee-rate", type=float, default=0.0010)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    parser.add_argument("--edge-hurdle", type=float, default=0.0050)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    raw = load_market_bars(args.data_root, args.symbol)
    derived = add_rl_derived_features(raw)
    decisions = build_decision_frame(derived, args.decision_interval_bars)
    decisions["dt"] = _timestamps(decisions["open_time"])
    for horizon in (1, 24, 72):
        decisions[f"forward_{horizon}h"] = forward_open_return(decisions, horizon)

    report = run_baselines(
        decisions,
        position_fraction=args.position_fraction,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        edge_hurdle=args.edge_hurdle,
    )
    report.update(
        {
            "symbol": args.symbol,
            "decision_interval_bars": args.decision_interval_bars,
            "position_fraction": args.position_fraction,
            "fee_rate": args.fee_rate,
            "slippage_rate": args.slippage_rate,
            "edge_hurdle": args.edge_hurdle,
            "outer_windows": [list(window) for window in OUTER_WINDOWS],
        }
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def run_baselines(
    decisions: pd.DataFrame,
    *,
    position_fraction: float,
    fee_rate: float,
    slippage_rate: float,
    edge_hurdle: float,
) -> dict:
    fold_reports = []
    all_returns: dict[str, list[float]] = {}
    for fold_id, (train_start, train_end, test_start, test_end) in enumerate(OUTER_WINDOWS, start=1):
        train = _eligible_window(decisions, train_start, train_end, max_horizon_hours=72)
        test = _eligible_window(decisions, test_start, test_end, max_horizon_hours=72)
        scaler = fit_feature_scaler(train, SCALED_BASE_COLUMNS)
        train_scaled = apply_feature_scaler(train, scaler)
        test_scaled = apply_feature_scaler(test, scaler)
        feature_columns = FEATURE_SETS[FEATURE_SET_MARKET_V2]

        ridge_predictions = {}
        ridge_ic = {}
        for horizon in (24, 72):
            target = f"forward_{horizon}h"
            model = Ridge(alpha=10.0)
            model.fit(train_scaled[list(feature_columns)], train_scaled[target])
            prediction = model.predict(test_scaled[list(feature_columns)])
            ridge_predictions[horizon] = prediction
            ridge_ic[horizon] = _spearman(prediction, test_scaled[target].to_numpy(dtype=float))

        strategy_specs: dict[str, tuple[np.ndarray, int, Callable[[float], int]]] = {
            "trend_72h": (
                test["market_trend_score"].to_numpy(dtype=float),
                72,
                lambda score: 1 if score >= 4 / 7 else -1 if score <= -4 / 7 else 0,
            ),
            "return1_reversal_1h": (
                test["return_1"].to_numpy(dtype=float),
                1,
                lambda score: -1 if score >= 0.005 else 1 if score <= -0.005 else 0,
            ),
            "momentum_24h": (
                test["1h_ret_6_z"].to_numpy(dtype=float),
                24,
                lambda score: 1 if score >= 1.0 else -1 if score <= -1.0 else 0,
            ),
            "ridge_24h": (
                ridge_predictions[24],
                24,
                lambda score: 1 if score >= edge_hurdle else -1 if score <= -edge_hurdle else 0,
            ),
            "ridge_72h": (
                ridge_predictions[72],
                72,
                lambda score: 1 if score >= edge_hurdle else -1 if score <= -edge_hurdle else 0,
            ),
        }
        strategies = {}
        for name, (scores, horizon, side_from_score) in strategy_specs.items():
            result = simulate_non_overlapping(
                test,
                scores,
                horizon_hours=horizon,
                side_from_score=side_from_score,
                position_fraction=position_fraction,
                one_way_cost=fee_rate + slippage_rate,
            )
            strategies[name] = {key: value for key, value in result.items() if key != "trade_returns"}
            all_returns.setdefault(name, []).extend(result["trade_returns"])

        fold_reports.append(
            {
                "fold": fold_id,
                "train_window": {"start": train_start, "end": train_end},
                "test_window": {"start": test_start, "end": test_end},
                "train_rows": len(train),
                "test_rows": len(test),
                "ridge_ic": {f"{key}h": value for key, value in ridge_ic.items()},
                "comparators": hold_comparators(
                    test,
                    position_fraction=position_fraction,
                    one_way_cost=fee_rate + slippage_rate,
                ),
                "strategies": strategies,
            }
        )

    aggregate = {}
    for name, returns in all_returns.items():
        fold_equities = [float(fold["strategies"][name]["final_equity"]) for fold in fold_reports]
        values = np.asarray(returns, dtype=float)
        lower, upper = moving_block_bootstrap_mean_ci(values, replications=10_000, seed=17)
        gross_profit = float(values[values > 0].sum()) if len(values) else 0.0
        gross_loss = float(values[values < 0].sum()) if len(values) else 0.0
        profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else 999.0 if gross_profit > 0 else 0.0
        aggregate[name] = {
            "final_equity": float(np.prod(fold_equities)),
            "profitable_folds": int(sum(equity > 1.0 for equity in fold_equities)),
            "fold_equities": fold_equities,
            "trades": len(values),
            "mean_trade_return": float(values.mean()) if len(values) else 0.0,
            "mean_trade_return_ci_95": [lower, upper],
            "profit_factor": profit_factor,
            "gate_pass": bool(
                np.prod(fold_equities) > 1.0
                and sum(equity > 1.0 for equity in fold_equities) >= 3
                and len(values) >= 100
                and profit_factor >= 1.10
                and lower > 0.0
            ),
        }
    return {"folds": fold_reports, "aggregate": aggregate}


def forward_open_return(decisions: pd.DataFrame, horizon_hours: int) -> pd.Series:
    entry = decisions["open"].shift(-1)
    exit_price = decisions["open"].shift(-(horizon_hours + 1))
    return exit_price / entry - 1.0


def simulate_non_overlapping(
    frame: pd.DataFrame,
    scores: np.ndarray,
    *,
    horizon_hours: int,
    side_from_score: Callable[[float], int],
    position_fraction: float,
    one_way_cost: float,
) -> dict:
    trade_returns = []
    long_trades = 0
    short_trades = 0
    next_entry = 0
    target_column = f"forward_{horizon_hours}h"
    for index, score in enumerate(scores):
        if index < next_entry:
            continue
        side = side_from_score(float(score))
        if side == 0:
            continue
        gross_return = float(frame.iloc[index][target_column]) * side
        net_equity_return = position_fraction * gross_return - 2.0 * position_fraction * one_way_cost
        trade_returns.append(net_equity_return)
        long_trades += int(side > 0)
        short_trades += int(side < 0)
        next_entry = index + horizon_hours
    return trade_metrics(trade_returns, long_trades=long_trades, short_trades=short_trades)


def trade_metrics(returns: list[float], *, long_trades: int, short_trades: int) -> dict:
    values = np.asarray(returns, dtype=float)
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    gross_profit = float(values[values > 0].sum()) if len(values) else 0.0
    gross_loss = float(values[values < 0].sum()) if len(values) else 0.0
    return {
        "trades": len(values),
        "long_trades": long_trades,
        "short_trades": short_trades,
        "win_rate": float((values > 0).mean()) if len(values) else 0.0,
        "mean_trade_return": float(values.mean()) if len(values) else 0.0,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss < 0 else 999.0 if gross_profit > 0 else 0.0,
        "final_equity": float(equity),
        "max_drawdown": float(max_drawdown),
        "trade_returns": values.tolist(),
    }


def hold_comparators(frame: pd.DataFrame, *, position_fraction: float, one_way_cost: float) -> dict:
    if len(frame) < 2:
        return {"flat": 1.0, "buy_hold": 1.0, "short_hold": 1.0}
    gross = float(frame.iloc[-1]["close"] / frame.iloc[1]["open"] - 1.0)
    cost = 2.0 * position_fraction * one_way_cost
    return {
        "flat": 1.0,
        "buy_hold": float(1.0 + position_fraction * gross - cost),
        "short_hold": float(1.0 - position_fraction * gross - cost),
    }


def moving_block_bootstrap_mean_ci(values: np.ndarray, *, replications: int, seed: int) -> tuple[float, float]:
    if len(values) < 2:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    block = max(1, int(round(np.sqrt(len(values)))))
    means = np.empty(replications, dtype=float)
    for iteration in range(replications):
        sampled = []
        while len(sampled) < len(values):
            start = int(rng.integers(0, len(values)))
            sampled.extend(values[(start + np.arange(block)) % len(values)])
        means[iteration] = np.mean(sampled[: len(values)])
    return (float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975)))


def _eligible_window(frame: pd.DataFrame, start: str, end: str, *, max_horizon_hours: int) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    mask = (frame["dt"] >= start_ts) & (
        frame["dt"] + pd.Timedelta(hours=max_horizon_hours, minutes=5) < end_ts
    )
    return frame.loc[mask].reset_index(drop=True)


def _timestamps(series: pd.Series) -> pd.Series:
    if series.dtype.kind == "M":
        return pd.to_datetime(series)
    return pd.to_datetime(series, unit="ms")


def _spearman(left: np.ndarray, right: np.ndarray) -> float:
    value = pd.Series(left).corr(pd.Series(right), method="spearman")
    return 0.0 if not np.isfinite(value) else float(value)


if __name__ == "__main__":
    main()
