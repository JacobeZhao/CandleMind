"""Feature engineering and train-window scaling for RL observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .state_builder import add_basic_market_features

FEATURE_SET_V1 = "v1"
FEATURE_SET_PROB_V2 = "prob_v2"
FEATURE_SET_TREND_FOLLOW_V1 = "trend_follow_v1"
FEATURE_SET_MARKET_V2 = "market_v2"

SCALED_BASE_COLUMNS = (
    "prob_spread",
    "prob_confidence",
    "prob_entropy",
    "prob_spread_change_1",
    "atr_pct",
    "volume_z",
    "ema_align_score",
    "hurst",
    "monthly_sma_distance",
    "market_trend_score",
    "1h_ema_align_score",
    "4h_ema_align_score",
    "1d_ema_align_score",
    "1h_ema8_slope",
    "4h_ema8_slope",
    "1h_ret_6_z",
    "1h_adx",
    "4h_adx",
    "5m_vol_regime",
    "1h_hurst",
    "4h_hurst",
    "1h_autocorr_1",
    "4h_autocorr_3",
    "1h_vol_of_vol",
    "4h_vol_of_vol",
)

FEATURE_SETS: dict[str, tuple[str, ...]] = {
    FEATURE_SET_V1: ("long_prob", "short_prob", "return_1", "atr_pct", "volume_z"),
    FEATURE_SET_PROB_V2: (
        "long_prob",
        "short_prob",
        "prob_spread_z",
        "prob_confidence_z",
        "prob_entropy_z",
        "prob_spread_change_1_z",
        "atr_pct_z",
        "volume_z_z",
    ),
    FEATURE_SET_TREND_FOLLOW_V1: (
        "long_prob",
        "short_prob",
        "trend_long_allowed",
        "trend_short_allowed",
        "prob_spread_z",
        "prob_confidence_z",
        "prob_entropy_z",
        "prob_spread_change_1_z",
        "atr_pct_z",
        "volume_z_z",
        "ema_align_score_z",
        "hurst_z",
        "monthly_sma_distance_z",
    ),
    FEATURE_SET_MARKET_V2: (
        "return_1",
        "atr_pct_z",
        "volume_z_z",
        "market_trend_score_z",
        "1h_ema_align_score_z",
        "4h_ema_align_score_z",
        "1d_ema_align_score_z",
        "1h_ema8_slope_z",
        "4h_ema8_slope_z",
        "1h_ret_6_z_z",
        "1h_adx_z",
        "4h_adx_z",
        "5m_vol_regime_z",
        "1h_hurst_z",
        "4h_hurst_z",
        "1h_autocorr_1_z",
        "4h_autocorr_3_z",
        "1h_vol_of_vol_z",
        "4h_vol_of_vol_z",
        "monthly_sma_distance_z",
    ),
}


@dataclass(frozen=True)
class FeatureBuildResult:
    bars: pd.DataFrame
    feature_columns: tuple[str, ...]
    scaler: dict[str, Any] | None


def build_decision_frame(bars: pd.DataFrame, decision_interval_bars: int) -> pd.DataFrame:
    """Align completed observations with the immediate next base-bar execution.

    Each output row carries features and the close from the last completed base
    bar in a decision interval. For row ``j > 0``, ``open`` is the execution
    price immediately after the previous decision. This lets adjacent-row
    environments account for the complete holding interval without making the
    action wait until the next sampled candle.
    """
    if decision_interval_bars <= 0:
        raise ValueError("decision_interval_bars must be positive")
    if decision_interval_bars == 1:
        return bars.reset_index(drop=True).copy()

    anchors = np.arange(
        decision_interval_bars - 1,
        len(bars),
        decision_interval_bars,
        dtype=int,
    )
    if len(anchors) < 2:
        raise ValueError("bars must contain at least two complete decision intervals")

    out = bars.iloc[anchors].copy().reset_index(drop=True)
    execution_indices = anchors[:-1] + 1
    execution_column = "open" if "open" in bars.columns else "close"
    out.loc[1:, "open"] = bars.iloc[execution_indices][execution_column].to_numpy(dtype=float)
    return out


def build_feature_frame(
    bars: pd.DataFrame,
    *,
    feature_set: str = FEATURE_SET_PROB_V2,
    scaler: dict[str, Any] | None = None,
    output_start: str | None = None,
    output_end: str | None = None,
) -> FeatureBuildResult:
    """Add RL features and apply a train-window scaler."""
    out = add_rl_derived_features(bars)
    if output_start or output_end:
        timestamp = (
            out["open_time"]
            if out["open_time"].dtype.kind == "M"
            else pd.to_datetime(out["open_time"], unit="ms")
        )
        mask = pd.Series(True, index=out.index)
        if output_start:
            mask &= timestamp >= pd.Timestamp(output_start)
        if output_end:
            mask &= timestamp < pd.Timestamp(output_end)
        out = out[mask].reset_index(drop=True)
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"Unknown RL feature_set {feature_set!r}; available={sorted(FEATURE_SETS)}")

    fitted_scaler = scaler
    if feature_set != FEATURE_SET_V1:
        if fitted_scaler is None:
            fitted_scaler = fit_feature_scaler(out, SCALED_BASE_COLUMNS)
        out = apply_feature_scaler(out, fitted_scaler)

    columns = tuple(col for col in FEATURE_SETS[feature_set] if col in out.columns)
    missing = set(FEATURE_SETS[feature_set]) - set(columns)
    if missing:
        raise ValueError(f"Feature set {feature_set!r} missing columns: {sorted(missing)}")
    return FeatureBuildResult(bars=out, feature_columns=columns, scaler=fitted_scaler)


def add_rl_derived_features(bars: pd.DataFrame) -> pd.DataFrame:
    out = add_basic_market_features(bars)
    long_prob = _series(out, "long_prob", 0.5).clip(0.0, 1.0)
    short_prob = _series(out, "short_prob", 0.5).clip(0.0, 1.0)
    denom = (long_prob + short_prob).replace(0.0, np.nan)
    directional_p = (long_prob / denom).fillna(0.5).clip(1e-6, 1.0 - 1e-6)

    out["prob_spread"] = long_prob - short_prob
    out["prob_confidence"] = pd.concat([long_prob, short_prob], axis=1).max(axis=1)
    out["prob_entropy"] = -(directional_p * np.log(directional_p) + (1.0 - directional_p) * np.log(1.0 - directional_p))
    out["prob_spread_change_1"] = out["prob_spread"].diff().fillna(0.0)
    out["abs_return_1"] = _series(out, "return_1", 0.0).abs()

    out["ema_align_score"] = _first_existing(out, ("5m_ema_align_score", "ema_align_score"), 0.0)
    out["hurst"] = _first_existing(out, ("5m_hurst", "hurst"), 0.75)
    out["vol_regime"] = _first_existing(out, ("5m_vol_regime", "vol_regime"), 1.0)
    out["monthly_sma"] = out["close"].astype(float).rolling(8640, min_periods=500).mean()
    out["monthly_sma_distance"] = ((out["close"].astype(float) / out["monthly_sma"]) - 1.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)

    votes = pd.DataFrame(index=out.index)
    for col in (
        "1h_ema_align_score", "4h_ema_align_score", "1d_ema_align_score",
        "1h_ema8_slope", "4h_ema8_slope", "1h_ret_6_z",
    ):
        votes[col] = np.sign(_series(out, col, 0.0))
    votes["monthly_sma_distance"] = np.sign(out["monthly_sma_distance"])
    out["market_trend_score"] = votes.mean(axis=1)

    gap = out["prob_spread"]
    conf = out["prob_confidence"]
    ema = out["ema_align_score"]
    hurst = out["hurst"]
    vol = out["vol_regime"]
    monthly = out["monthly_sma_distance"]
    trend_ok = (hurst >= 0.50) & (vol < 2.0)
    out["trend_long_allowed"] = ((gap >= 0.06) & (conf >= 0.52) & (ema >= 0.0) & (monthly >= -0.03) & trend_ok).astype(float)
    out["trend_short_allowed"] = ((gap <= -0.06) & (conf >= 0.52) & (ema <= 0.0) & (monthly <= 0.03) & trend_ok).astype(float)
    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def fit_feature_scaler(bars: pd.DataFrame, columns: tuple[str, ...]) -> dict[str, Any]:
    features: dict[str, dict[str, float]] = {}
    for col in columns:
        s = _series(bars, col, 0.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
        lo = float(s.quantile(0.01))
        hi = float(s.quantile(0.99))
        clipped = s.clip(lo, hi)
        mean = float(clipped.mean())
        std = float(clipped.std())
        if not np.isfinite(std) or std < 1e-8:
            std = 1.0
        features[col] = {"mean": mean, "std": std, "p01": lo, "p99": hi}
    return {"version": "rl_feature_scaler_v1", "features": features}


def apply_feature_scaler(bars: pd.DataFrame, scaler: dict[str, Any]) -> pd.DataFrame:
    out = bars.copy()
    for col, stats in scaler.get("features", {}).items():
        s = _series(out, col, 0.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
        clipped = s.clip(float(stats["p01"]), float(stats["p99"]))
        z = (clipped - float(stats["mean"])) / max(float(stats["std"]), 1e-8)
        out[f"{col}_z"] = z.clip(-5.0, 5.0)
    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _series(df: pd.DataFrame, col: str, default: float) -> pd.Series:
    if col in df.columns:
        return df[col].astype(float)
    return pd.Series(default, index=df.index, dtype=float)


def _first_existing(df: pd.DataFrame, columns: tuple[str, ...], default: float) -> pd.Series:
    for col in columns:
        if col in df.columns:
            return df[col].astype(float)
    return pd.Series(default, index=df.index, dtype=float)
