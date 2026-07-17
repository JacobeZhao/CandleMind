"""Observation construction for the RL trading environment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_SIGNAL_COLUMNS = ("long_prob", "short_prob")
DEFAULT_MARKET_COLUMNS = ("return_1", "atr_pct", "volume_z")


@dataclass(frozen=True)
class PositionState:
    position: int
    unrealized_return: float
    bars_in_position: int
    equity_return: float
    drawdown: float


class StateBuilder:
    """Builds fixed-width numeric observations from market and position state."""

    def __init__(self, feature_columns: Iterable[str] | None = None):
        cols = tuple(feature_columns or ())
        self.feature_columns = cols or (DEFAULT_SIGNAL_COLUMNS + DEFAULT_MARKET_COLUMNS)

    @property
    def observation_size(self) -> int:
        return len(self.feature_columns) + 5

    def build(self, row: pd.Series, position_state: PositionState) -> np.ndarray:
        values = []
        for col in self.feature_columns:
            raw = row[col] if col in row else 0.0
            values.append(_finite_float(raw))
        values.extend(
            [
                float(position_state.position),
                _finite_float(position_state.unrealized_return),
                _finite_float(position_state.bars_in_position / 100.0),
                _finite_float(position_state.equity_return),
                _finite_float(position_state.drawdown),
            ]
        )
        return np.asarray(values, dtype=np.float32)


def add_basic_market_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add fallback numeric features when real ML feature columns are absent."""
    out = df.copy()
    close = out["close"].astype(float)
    if "return_1" not in out:
        out["return_1"] = close.pct_change().fillna(0.0)
    if "atr_pct" not in out:
        if "atr" in out:
            out["atr_pct"] = out["atr"].astype(float) / close.replace(0, np.nan)
        elif {"high", "low"}.issubset(out.columns):
            out["atr_pct"] = (out["high"].astype(float) - out["low"].astype(float)) / close.replace(0, np.nan)
        else:
            out["atr_pct"] = 0.0
        out["atr_pct"] = out["atr_pct"].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    if "volume_z" not in out:
        if "volume" in out:
            vol = out["volume"].astype(float)
            mean = vol.rolling(48, min_periods=4).mean()
            std = vol.rolling(48, min_periods=4).std().replace(0, np.nan)
            out["volume_z"] = ((vol - mean) / std).replace([np.inf, -np.inf], 0.0).fillna(0.0)
        else:
            out["volume_z"] = 0.0
    for col in DEFAULT_SIGNAL_COLUMNS:
        if col not in out:
            out[col] = 0.5
    return out


def _finite_float(value: object) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if not np.isfinite(v):
        return 0.0
    return v
