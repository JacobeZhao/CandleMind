"""Audit available taker, VWAP, and funding features at corrected decision times."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as parquet
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.feature_engineering import build_decision_frame
from backend.scripts.evaluation.rl_alpha_baseline import forward_open_return

KEYWORDS = ("taker", "funding", "vwap")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon-hours", nargs="+", type=int, default=[1, 4, 24, 72])
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    labels_path = args.data_root / "processed" / "labels" / f"{args.symbol}_5m_labels.parquet"
    features_path = args.data_root / "processed" / "features_ml" / f"{args.symbol}_features.parquet"
    schema = parquet.read_schema(features_path).names
    columns = [name for name in schema if any(keyword in name.lower() for keyword in KEYWORDS)]
    labels = pd.read_parquet(
        labels_path,
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    features = pd.read_parquet(features_path, columns=["open_time", *columns])
    bars = labels.merge(features, on="open_time", how="inner").sort_values("open_time").reset_index(drop=True)
    decisions = build_decision_frame(bars, 12)
    decisions["dt"] = _timestamps(decisions["open_time"])
    report = audit_feature_bundle(decisions, columns=columns, horizon_hours=args.horizon_hours)
    report.update({"symbol": args.symbol, "rows": len(decisions), "columns": columns})
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def audit_feature_bundle(frame: pd.DataFrame, *, columns: list[str], horizon_hours: list[int]) -> dict:
    horizons = {}
    for horizon in horizon_hours:
        target = forward_open_return(frame, horizon)
        valid = target.notna()
        target_values = target.loc[valid].to_numpy(dtype=float)
        month = frame.loc[valid, "dt"].dt.to_period("M")
        results = []
        pvalues = []
        for column in columns:
            values = frame.loc[valid, column].astype(float)
            if values.nunique(dropna=True) < 2:
                global_ic = 0.0
                pvalue = 1.0
            else:
                result = spearmanr(values, target_values, nan_policy="omit")
                global_ic = 0.0 if not np.isfinite(result.statistic) else float(result.statistic)
                pvalue = 1.0 if not np.isfinite(result.pvalue) else float(result.pvalue)
            monthly_ics = []
            for period in month.unique():
                mask = month == period
                if int(mask.sum()) < 100 or values.loc[mask.index[mask]].nunique(dropna=True) < 2:
                    continue
                monthly = spearmanr(values.loc[mask.index[mask]], target_values[mask.to_numpy()], nan_policy="omit").statistic
                if np.isfinite(monthly):
                    monthly_ics.append(float(monthly))
            sign = np.sign(global_ic)
            sign_consistency = (
                float(np.mean(np.sign(monthly_ics) == sign)) if monthly_ics and sign != 0 else 0.0
            )
            tail_spreads = {str(fraction): 0.0 for fraction in (0.01, 0.02, 0.05)}
            if values.nunique(dropna=True) < 2:
                bottom = top = 0.0
            else:
                quantiles = pd.qcut(
                    values.rank(method="average"),
                    10,
                    labels=False,
                    duplicates="drop",
                )
                bottom = float(np.mean(target_values[quantiles.to_numpy() == 0]))
                top = float(np.mean(target_values[quantiles.to_numpy() == int(quantiles.max())]))
                finite_values = values[np.isfinite(values)]
                for fraction in (0.01, 0.02, 0.05):
                    lower = float(finite_values.quantile(fraction))
                    upper = float(finite_values.quantile(1.0 - fraction))
                    lower_return = float(np.mean(target_values[values.to_numpy() <= lower]))
                    upper_return = float(np.mean(target_values[values.to_numpy() >= upper]))
                    tail_spreads[str(fraction)] = float(sign * (upper_return - lower_return))
            results.append(
                {
                    "feature": column,
                    "global_ic": global_ic,
                    "pvalue": pvalue,
                    "monthly_ic_median": float(np.median(monthly_ics)) if monthly_ics else 0.0,
                    "monthly_sign_consistency": sign_consistency,
                    "months": len(monthly_ics),
                    "top_bottom_return_spread": top - bottom,
                    "directional_decile_spread": float(sign * (top - bottom)),
                    "directional_tail_spreads": tail_spreads,
                    "missing_rate": float(values.isna().mean()),
                    "zero_rate": float((values.fillna(0.0) == 0.0).mean()),
                }
            )
            pvalues.append(pvalue)
        qvalues = benjamini_hochberg(np.asarray(pvalues, dtype=float))
        for item, qvalue in zip(results, qvalues):
            item["qvalue"] = float(qvalue)
            item["qualified"] = bool(
                abs(item["global_ic"]) >= 0.02
                and item["monthly_sign_consistency"] >= 0.60
                and item["directional_decile_spread"] > 0.0
                and qvalue < 0.05
            )
        results.sort(key=lambda item: abs(item["global_ic"]), reverse=True)
        horizons[f"{horizon}h"] = {
            "qualified_features": [item["feature"] for item in results if item["qualified"]],
            "features": results,
        }
    return {"horizons": horizons}


def benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    if len(pvalues) == 0:
        return pvalues.copy()
    order = np.argsort(pvalues)
    ranked = pvalues[order]
    adjusted = ranked * len(ranked) / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    output = np.empty_like(adjusted)
    output[order] = np.clip(adjusted, 0.0, 1.0)
    return output


def _timestamps(series: pd.Series) -> pd.Series:
    if series.dtype.kind == "M":
        return pd.to_datetime(series)
    return pd.to_datetime(series, unit="ms")


if __name__ == "__main__":
    main()
