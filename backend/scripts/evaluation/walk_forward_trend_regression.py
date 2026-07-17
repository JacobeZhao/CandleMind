"""Walk-forward audit for causal trend regression and non-overlapping trades."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor, early_stopping, log_evaluation
from scipy.stats import spearmanr

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.datastore import REPORTS_DIR
from scripts.evaluation.audit_trend_regression import model_params, select_by_gain
from scripts.training.retrain_multi_horizon import HORIZON_BARS, _exclude_cols, load_merged


def simulate_non_overlapping(
    frame: pd.DataFrame,
    prediction: np.ndarray,
    *,
    long_threshold: float,
    short_threshold: float,
    holding_bars: int,
    position_fraction: float,
) -> dict:
    trades = []
    next_entry = 0
    for i, score in enumerate(prediction):
        if i < next_entry:
            continue
        if score >= long_threshold:
            direction = 1
            net_return = float(frame.iloc[i]["long_net_return"])
        elif score <= short_threshold:
            direction = -1
            net_return = float(frame.iloc[i]["short_net_return"])
        else:
            continue
        trades.append({
            "open_time": str(frame.iloc[i]["open_time"]),
            "direction": direction,
            "score": float(score),
            "net_return": net_return,
        })
        next_entry = i + holding_bars

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for trade in trades:
        equity *= max(0.0, 1.0 + trade["net_return"] * position_fraction)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    returns = np.asarray([trade["net_return"] for trade in trades], dtype=float)
    return {
        "trades": len(trades),
        "long_trades": sum(trade["direction"] == 1 for trade in trades),
        "short_trades": sum(trade["direction"] == -1 for trade in trades),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "mean_net_return": float(returns.mean()) if len(returns) else 0.0,
        "total_net_return": float(returns.sum()) if len(returns) else 0.0,
        "final_equity": equity,
        "max_drawdown": max_drawdown,
        "trade_rows": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", default="4h", choices=HORIZON_BARS)
    parser.add_argument("--variant", default="trend_v1")
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--position-fraction", type=float, default=0.5)
    args = parser.parse_args()

    df, _ = load_merged(args.symbol, args.horizon, "long", args.variant)
    df["dt"] = df["open_time"]
    columns = [
        c for c in df.columns
        if c not in _exclude_cols("long", args.horizon) and c != "dt"
    ]
    last_complete_month = df["dt"].max().to_period("M").to_timestamp()
    validation_starts = pd.date_range(
        end=last_complete_month - pd.DateOffset(months=1),
        periods=args.folds,
        freq="MS",
    )
    fold_reports = []
    all_trades = []

    for fold_id, val_start in enumerate(validation_starts, start=1):
        val_end = val_start + pd.DateOffset(months=1)
        calibration_start = val_start - pd.DateOffset(months=1)
        train_start = calibration_start - pd.DateOffset(months=6)
        train = df[(df["dt"] >= train_start) & (df["dt"] < calibration_start)]
        calibration = df[(df["dt"] >= calibration_start) & (df["dt"] < val_start)]
        validation = df[(df["dt"] >= val_start) & (df["dt"] < val_end)]
        purge = HORIZON_BARS[args.horizon]
        train = train.iloc[:-purge].reset_index(drop=True)
        calibration = calibration.iloc[:-purge].reset_index(drop=True)
        validation = validation.reset_index(drop=True)

        X_train = train[columns].fillna(0).astype(np.float32)
        X_cal = calibration[columns].fillna(0).astype(np.float32)
        X_val = validation[columns].fillna(0).astype(np.float32)
        y_train = train["trend_score"].fillna(0).astype(np.float32)
        y_cal = calibration["trend_score"].fillna(0).astype(np.float32)
        y_val = validation["trend_score"].fillna(0).astype(np.float32)
        selected = select_by_gain(X_train, y_train)

        model = LGBMRegressor(**model_params())
        model.fit(
            X_train[selected],
            y_train,
            eval_set=[(X_cal[selected], y_cal)],
            callbacks=[early_stopping(50, verbose=False), log_evaluation(0)],
        )
        cal_prediction = model.predict(X_cal[selected])
        val_prediction = model.predict(X_val[selected])

        strategies = {}
        for fraction in (0.01, 0.02, 0.05):
            strategy = simulate_non_overlapping(
                validation,
                val_prediction,
                long_threshold=float(np.quantile(cal_prediction, 1.0 - fraction)),
                short_threshold=float(np.quantile(cal_prediction, fraction)),
                holding_bars=HORIZON_BARS[args.horizon],
                position_fraction=args.position_fraction,
            )
            strategies[str(fraction)] = {k: v for k, v in strategy.items() if k != "trade_rows"}
            for trade in strategy["trade_rows"]:
                all_trades.append({"fold": fold_id, "fraction": fraction, **trade})

        fold_reports.append({
            "fold": fold_id,
            "train_start": str(train["dt"].min()),
            "train_end": str(train["dt"].max()),
            "calibration_start": str(calibration["dt"].min()),
            "calibration_end": str(calibration["dt"].max()),
            "validation_start": str(validation["dt"].min()),
            "validation_end": str(validation["dt"].max()),
            "n_train": len(train),
            "n_calibration": len(calibration),
            "n_validation": len(validation),
            "forward_return_ic": float(spearmanr(val_prediction, validation["forward_return"]).statistic),
            "feature_count": len(selected),
            "strategies": strategies,
        })

    aggregate = {}
    trades_frame = pd.DataFrame(all_trades)
    for fraction in (0.01, 0.02, 0.05):
        selected_trades = trades_frame[trades_frame["fraction"] == fraction]
        returns = selected_trades["net_return"].to_numpy(dtype=float)
        equity = float(np.prod(1.0 + returns * args.position_fraction)) if len(returns) else 1.0
        aggregate[str(fraction)] = {
            "trades": len(returns),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "mean_net_return": float(returns.mean()) if len(returns) else 0.0,
            "total_net_return": float(returns.sum()) if len(returns) else 0.0,
            "final_equity": equity,
        }

    report = {
        "symbol": args.symbol,
        "horizon": args.horizon,
        "variant": args.variant,
        "folds": args.folds,
        "position_fraction": args.position_fraction,
        "fold_reports": fold_reports,
        "aggregate": aggregate,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"trend_walk_forward_{args.symbol}_{args.horizon}_{args.variant}"
    report_path = REPORTS_DIR / f"{stem}.json"
    trades_path = REPORTS_DIR / f"{stem}_trades.parquet"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    trades_frame.to_parquet(trades_path, index=False)
    print(json.dumps({"report": str(report_path), "trades": str(trades_path), **report}, indent=2))


if __name__ == "__main__":
    main()
