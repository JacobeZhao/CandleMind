"""Audit causal multi-horizon labels with a strict temporal holdout."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import average_precision_score, roc_auc_score

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.datastore import REPORTS_DIR
from app.services.trend_predictor import train_lgbm
from scripts.training.retrain_multi_horizon import (
    HORIZON_BARS,
    _exclude_cols,
    load_merged,
    select_features_by_gain,
)


def audit_side(symbol: str, horizon: str, side: str, variant: str) -> dict:
    df, target = load_merged(symbol, horizon, side, variant)
    df["dt"] = df["open_time"]
    last_dt = df["dt"].max()
    val_start = last_dt - pd.DateOffset(months=1)
    train_start = val_start - pd.DateOffset(months=6)

    train = df[(df["dt"] >= train_start) & (df["dt"] < val_start)]
    train = train.iloc[: -HORIZON_BARS[horizon]].reset_index(drop=True)
    val = df[df["dt"] >= val_start].reset_index(drop=True)

    columns = [
        c for c in df.columns
        if c not in _exclude_cols(side, horizon) and c != "dt"
    ]
    X_train = train[columns].fillna(0).astype(np.float32)
    y_train = train[target].astype(int)
    X_val = val[columns].fillna(0).astype(np.float32)
    y_val = val[target].astype(int)
    columns = select_features_by_gain(X_train, y_train, max_features=100)

    model = train_lgbm(X_train[columns], y_train, X_val[columns], y_val)
    probability = model.predict_proba(X_val[columns])[:, 1]
    ic = float(spearmanr(probability, y_val).statistic)
    top_cutoff = float(np.quantile(probability, 0.9))
    top_mask = probability >= top_cutoff
    net_column = f"{side}_net_return"
    top_net = val.loc[top_mask, net_column] if net_column in val else pd.Series(dtype=float)

    return {
        "side": side,
        "n_train": len(train),
        "n_val": len(val),
        "train_start": str(train["dt"].min()),
        "train_end": str(train["dt"].max()),
        "val_start": str(val["dt"].min()),
        "val_end": str(val["dt"].max()),
        "positive_rate_train": float(y_train.mean()),
        "positive_rate_val": float(y_val.mean()),
        "auc": float(roc_auc_score(y_val, probability)),
        "average_precision": float(average_precision_score(y_val, probability)),
        "ic": ic,
        "direction_accuracy": float(((probability > 0.5) == y_val.to_numpy()).mean()),
        "top_decile_probability": top_cutoff,
        "top_decile_rows": int(top_mask.sum()),
        "top_decile_positive_rate": float(y_val.to_numpy()[top_mask].mean()),
        "top_decile_mean_net_return": float(top_net.mean()) if len(top_net) else None,
        "top_decile_total_net_return": float(top_net.sum()) if len(top_net) else None,
        "feature_count": len(columns),
        "top_features": columns[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", default="1h", choices=HORIZON_BARS)
    parser.add_argument("--variant", default="causal_v3")
    parser.add_argument("--side", default="both", choices=("long", "short", "both"))
    args = parser.parse_args()

    sides = ("long", "short") if args.side == "both" else (args.side,)
    report = {
        "symbol": args.symbol,
        "horizon": args.horizon,
        "variant": args.variant,
        "method": "6m_train_1m_holdout_purged_lgbm",
        "results": [
            audit_side(args.symbol, args.horizon, side, args.variant)
            for side in sides
        ],
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output = REPORTS_DIR / f"signal_audit_{args.symbol}_{args.horizon}_{args.variant}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **report}, indent=2))


if __name__ == "__main__":
    main()
