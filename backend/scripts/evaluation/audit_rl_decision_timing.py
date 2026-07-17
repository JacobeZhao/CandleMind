"""Audit stale row-stride observations against immediate-next-bar decisions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.data import MARKET_V2_FEATURE_COLUMNS
from backend.app.rl.feature_engineering import FEATURE_SET_MARKET_V2, build_feature_frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--data-root", type=Path, default=Path(os.getenv("MARKET_DATA_DIR", "G:/CandleMind/CandleMind_data")))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--decision-interval-bars", type=int, default=12)
    parser.add_argument("--base-bar-minutes", type=int, default=5)
    parser.add_argument("--horizon-hours", nargs="+", type=int, default=[1, 4, 24])
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    bars = _synthetic_bars() if args.synthetic else load_market_bars(args.data_root, args.symbol)
    bars = _slice_time(bars, args.start, args.end)
    feature_result = build_feature_frame(bars, feature_set=FEATURE_SET_MARKET_V2)
    features = feature_result.bars
    report = audit_timing(
        features,
        feature_columns=feature_result.feature_columns,
        decision_interval_bars=args.decision_interval_bars,
        base_bar_minutes=args.base_bar_minutes,
        horizon_hours=args.horizon_hours,
    )
    report.update({"symbol": args.symbol, "synthetic": args.synthetic, "rows": len(features)})
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def audit_timing(
    bars: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...],
    decision_interval_bars: int,
    base_bar_minutes: int,
    horizon_hours: list[int],
) -> dict:
    if decision_interval_bars <= 1:
        raise ValueError("timing audit requires decision_interval_bars greater than one")
    if base_bar_minutes <= 0:
        raise ValueError("base_bar_minutes must be positive")
    timestamp = _timestamps(bars["open_time"])
    opens = bars["open"].to_numpy(dtype=float)
    entry_indices = np.arange(decision_interval_bars, len(bars), decision_interval_bars)
    current_obs = entry_indices - decision_interval_bars
    corrected_obs = entry_indices - 1
    completion_delta = pd.Timedelta(minutes=base_bar_minutes)
    current_lag = timestamp.iloc[entry_indices].reset_index(drop=True) - (
        timestamp.iloc[current_obs].reset_index(drop=True) + completion_delta
    )
    corrected_lag = timestamp.iloc[entry_indices].reset_index(drop=True) - (
        timestamp.iloc[corrected_obs].reset_index(drop=True) + completion_delta
    )

    horizon_reports = {}
    for hours in horizon_hours:
        horizon_bars = hours * 60 // base_bar_minutes
        valid = entry_indices + horizon_bars < len(bars)
        entries = entry_indices[valid]
        old_rows = current_obs[valid]
        new_rows = corrected_obs[valid]
        returns = opens[entries + horizon_bars] / opens[entries] - 1.0
        feature_results = []
        for column in feature_columns:
            old_ic = _spearman(bars.iloc[old_rows][column], returns)
            new_ic = _spearman(bars.iloc[new_rows][column], returns)
            feature_results.append(
                {
                    "feature": column,
                    "legacy_ic": old_ic,
                    "corrected_ic": new_ic,
                    "absolute_ic_change": abs(new_ic) - abs(old_ic),
                }
            )
        feature_results.sort(key=lambda item: abs(item["corrected_ic"]), reverse=True)
        horizon_reports[f"{hours}h"] = {
            "samples": len(returns),
            "mean_forward_return": float(np.mean(returns)),
            "top_corrected_features": feature_results[:10],
        }

    return {
        "decision_interval_bars": decision_interval_bars,
        "base_bar_minutes": base_bar_minutes,
        "decision_samples": len(entry_indices),
        "legacy_observation_lag_minutes": float(current_lag.dt.total_seconds().median() / 60.0),
        "corrected_observation_lag_minutes": float(corrected_lag.dt.total_seconds().median() / 60.0),
        "horizons": horizon_reports,
    }


def load_market_bars(data_root: Path, symbol: str) -> pd.DataFrame:
    labels_path = data_root / "processed" / "labels" / f"{symbol}_5m_labels.parquet"
    features_path = data_root / "processed" / "features_ml" / f"{symbol}_features.parquet"
    labels = pd.read_parquet(
        labels_path,
        columns=["open_time", "open", "high", "low", "close", "volume", "atr"],
    )
    import pyarrow.parquet as parquet

    available = set(parquet.read_schema(features_path).names)
    feature_columns = [column for column in MARKET_V2_FEATURE_COLUMNS if column in available]
    features = pd.read_parquet(features_path, columns=feature_columns)
    return labels.merge(features, on="open_time", how="inner").sort_values("open_time").reset_index(drop=True)


def _slice_time(bars: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    timestamp = _timestamps(bars["open_time"])
    mask = pd.Series(True, index=bars.index)
    if start:
        mask &= timestamp >= pd.Timestamp(start)
    if end:
        mask &= timestamp < pd.Timestamp(end)
    return bars.loc[mask].reset_index(drop=True)


def _timestamps(series: pd.Series) -> pd.Series:
    if series.dtype.kind == "M":
        return pd.to_datetime(series).reset_index(drop=True)
    return pd.to_datetime(series, unit="ms").reset_index(drop=True)


def _spearman(values: pd.Series, returns: np.ndarray) -> float:
    value_series = pd.Series(values.to_numpy(dtype=float))
    return_series = pd.Series(returns)
    if value_series.nunique(dropna=True) < 2 or return_series.nunique(dropna=True) < 2:
        return 0.0
    result = value_series.corr(return_series, method="spearman")
    return 0.0 if not np.isfinite(result) else float(result)


def _synthetic_bars(rows: int = 12_000) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.001, rows)
    close = 100.0 * np.exp(np.cumsum(returns))
    open_price = np.concatenate(([100.0], close[:-1]))
    return pd.DataFrame(
        {
            "open_time": pd.date_range("2024-01-01", periods=rows, freq="5min"),
            "open": open_price,
            "high": np.maximum(open_price, close) * 1.0005,
            "low": np.minimum(open_price, close) * 0.9995,
            "close": close,
            "volume": rng.lognormal(5.0, 0.5, rows),
            "atr": np.abs(close - open_price),
            "1h_ema_align_score": pd.Series(close).pct_change(12).fillna(0.0),
            "4h_ema_align_score": pd.Series(close).pct_change(48).fillna(0.0),
            "1d_ema_align_score": pd.Series(close).pct_change(288).fillna(0.0),
        }
    )


if __name__ == "__main__":
    main()
