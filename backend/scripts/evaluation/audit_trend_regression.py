"""Audit a causal trend-score regressor on a purged temporal holdout."""

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
from app.services.trend_predictor import _lgb_device
from scripts.training.retrain_multi_horizon import HORIZON_BARS, _exclude_cols, load_merged


def model_params() -> dict:
    return {
        "objective": "huber",
        "n_estimators": 800,
        "learning_rate": 0.03,
        "num_leaves": 31,
        "max_depth": 6,
        "min_child_samples": 200,
        "subsample": 0.8,
        "colsample_bytree": 0.6,
        "reg_alpha": 1.0,
        "reg_lambda": 3.0,
        "random_state": 42,
        "n_jobs": 4,
        "verbose": -1,
        "device": _lgb_device(),
    }


def select_by_gain(X: pd.DataFrame, y: pd.Series, max_features: int = 100) -> list[str]:
    columns = X.columns[X.var() > 1e-12].tolist()
    params = model_params()
    params.update({"n_estimators": 250, "learning_rate": 0.05})
    probe = LGBMRegressor(**params).fit(X[columns], y)
    gain = probe.booster_.feature_importance(importance_type="gain")
    ranked = sorted(zip(columns, gain), key=lambda item: item[1], reverse=True)
    return [name for name, importance in ranked if importance > 0][:max_features]


def selection_metrics(val: pd.DataFrame, prediction: np.ndarray) -> list[dict]:
    rows = []
    order = np.argsort(prediction)
    for fraction in (0.01, 0.02, 0.05, 0.10, 0.20):
        count = max(1, int(len(val) * fraction))
        long_idx = order[-count:]
        short_idx = order[:count]
        rows.append({
            "fraction": fraction,
            "count": count,
            "long_mean_net_return": float(val.iloc[long_idx]["long_net_return"].mean()),
            "long_win_rate": float((val.iloc[long_idx]["long_net_return"] > 0).mean()),
            "short_mean_net_return": float(val.iloc[short_idx]["short_net_return"].mean()),
            "short_win_rate": float((val.iloc[short_idx]["short_net_return"] > 0).mean()),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", default="1h", choices=HORIZON_BARS)
    parser.add_argument("--variant", default="trend_v1")
    args = parser.parse_args()

    df, _ = load_merged(args.symbol, args.horizon, "long", args.variant)
    df["dt"] = df["open_time"]
    last_dt = df["dt"].max()
    val_start = last_dt - pd.DateOffset(months=1)
    train_start = val_start - pd.DateOffset(months=6)
    train = df[(df["dt"] >= train_start) & (df["dt"] < val_start)]
    train = train.iloc[: -HORIZON_BARS[args.horizon]].reset_index(drop=True)
    val = df[df["dt"] >= val_start].reset_index(drop=True)

    columns = [
        c for c in df.columns
        if c not in _exclude_cols("long", args.horizon) and c != "dt"
    ]
    X_train = train[columns].fillna(0).astype(np.float32)
    X_val = val[columns].fillna(0).astype(np.float32)
    y_train = train["trend_score"].fillna(0).astype(np.float32)
    y_val = val["trend_score"].fillna(0).astype(np.float32)
    columns = select_by_gain(X_train, y_train)

    model = LGBMRegressor(**model_params())
    model.fit(
        X_train[columns],
        y_train,
        eval_set=[(X_val[columns], y_val)],
        callbacks=[early_stopping(50, verbose=False), log_evaluation(0)],
    )
    prediction = model.predict(X_val[columns])
    report = {
        "symbol": args.symbol,
        "horizon": args.horizon,
        "variant": args.variant,
        "method": "6m_train_1m_holdout_purged_huber_regression",
        "n_train": len(train),
        "n_val": len(val),
        "spearman_trend_score": float(spearmanr(prediction, y_val).statistic),
        "spearman_forward_return": float(spearmanr(prediction, val["forward_return"]).statistic),
        "feature_count": len(columns),
        "top_features": columns[:20],
        "selection_metrics": selection_metrics(val, prediction),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"trend_regression_audit_{args.symbol}_{args.horizon}_{args.variant}"
    report_path = REPORTS_DIR / f"{stem}.json"
    score_path = REPORTS_DIR / f"{stem}_scores.parquet"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    pd.DataFrame({
        "open_time": val["open_time"],
        "prediction": prediction.astype(np.float32),
        "trend_score": y_val,
        "forward_return": val["forward_return"],
        "long_net_return": val["long_net_return"],
        "short_net_return": val["short_net_return"],
    }).to_parquet(score_path, index=False)
    print(json.dumps({"report": str(report_path), "scores": str(score_path), **report}, indent=2))


if __name__ == "__main__":
    main()
