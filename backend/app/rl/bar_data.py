"""Shared market-bar validation for RL environments."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .state_builder import add_basic_market_features


def prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    out = add_basic_market_features(bars)
    if "open" not in out:
        out["open"] = out["close"]
    out = out.replace([np.inf, -np.inf], np.nan).fillna(0.0).reset_index(drop=True)
    out["close"] = out["close"].astype(float)
    out["open"] = out["open"].astype(float)
    if (out[["open", "close"]] <= 0).any().any():
        raise ValueError("open and close prices must be positive")
    return out
