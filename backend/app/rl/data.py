"""Data loading adapters for the RL decision layer."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .feature_engineering import FEATURE_SETS, FEATURE_SET_MARKET_V2, FEATURE_SET_PROB_V2

MARKET_V2_FEATURE_COLUMNS = (
    "open_time",
    "1h_ema_align_score", "4h_ema_align_score", "1d_ema_align_score",
    "1h_ema8_slope", "4h_ema8_slope", "1h_ret_6_z",
    "1h_adx", "4h_adx", "5m_vol_regime", "1h_hurst", "4h_hurst",
    "1h_autocorr_1", "4h_autocorr_3", "1h_vol_of_vol", "4h_vol_of_vol",
    "5m_funding_rate",
)


def attach_funding_cashflow(bars: pd.DataFrame) -> pd.DataFrame:
    """Expose the historical 8-hour event rate under the environment contract."""
    out = bars.copy()
    if "5m_funding_rate" in out.columns:
        out["funding_rate"] = pd.to_numeric(
            out["5m_funding_rate"], errors="coerce"
        ).fillna(0.0)
    return out


def load_ml_scored_bars(symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Load historical bars with existing ML trend probabilities.

    This delegates to the supervised trend stack. It can require optional model
    dependencies such as LightGBM/CatBoost, so synthetic tests should remain the
    default health check for the RL environment itself.
    """
    from backend.app.services.ml_strategy import load_scored_bars

    bars = load_scored_bars(symbol, start=start, end=end)
    required = {"close", "long_prob", "short_prob"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"ML scored bars missing required columns: {sorted(missing)}")
    return attach_funding_cashflow(bars)


def load_market_bars(symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Load OHLCV and engineered market features without model probabilities."""
    from backend.app.datastore import FEATURES_ML_DIR, LABELS_DIR

    labels = pd.read_parquet(
        LABELS_DIR / f"{symbol}_5m_labels.parquet",
        columns=["open_time", "open", "high", "low", "close", "volume", "atr"],
    )
    features = pd.read_parquet(
        FEATURES_ML_DIR / f"{symbol}_features.parquet",
        columns=list(MARKET_V2_FEATURE_COLUMNS),
    )
    for frame in (labels, features):
        timestamp = (
            frame["open_time"]
            if frame["open_time"].dtype.kind == "M"
            else pd.to_datetime(frame["open_time"], unit="ms")
        )
        mask = pd.Series(True, index=frame.index)
        if start:
            mask &= timestamp >= pd.Timestamp(start)
        if end:
            mask &= timestamp < pd.Timestamp(end)
        frame.drop(index=frame.index[~mask], inplace=True)
    bars = labels.merge(features, on="open_time", how="inner", suffixes=("", "_feat"))
    return attach_funding_cashflow(bars).reset_index(drop=True)


def load_bars_for_feature_set(
    symbol: str,
    *,
    start: Optional[str],
    end: Optional[str],
    feature_set: str,
) -> pd.DataFrame:
    if feature_set == FEATURE_SET_MARKET_V2:
        return load_market_bars(symbol, start=start, end=end)
    return load_ml_scored_bars(symbol, start=start, end=end)


def select_feature_columns(bars: pd.DataFrame, limit: int = 32, feature_set: str = FEATURE_SET_PROB_V2) -> tuple[str, ...]:
    """Return the configured RL observation columns.

    Feature preparation happens in feature_engineering.build_feature_frame.
    long_prob/short_prob stay first for baseline compatibility.
    """
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"Unknown feature_set {feature_set!r}; available={sorted(FEATURE_SETS)}")
    return FEATURE_SETS[feature_set]
