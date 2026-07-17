"""Reward calculation for RL trading simulation."""

from __future__ import annotations

import numpy as np

from .config import RewardConfig


def compute_reward(
    *,
    equity_before: float,
    equity_after: float,
    peak_equity_before: float,
    invalid_action: bool,
    flipped: bool,
    config: RewardConfig,
) -> float:
    if equity_before <= 0 or equity_after <= 0:
        pnl_component = 0.0
    else:
        pnl_component = np.log(equity_after / equity_before) * config.pnl_scale

    peak_before = max(peak_equity_before, equity_before)
    peak_after = max(peak_before, equity_after)
    drawdown_before = 0.0 if peak_before <= 0 else max(0.0, 1.0 - equity_before / peak_before)
    drawdown_after = 0.0 if peak_after <= 0 else max(0.0, 1.0 - equity_after / peak_after)
    drawdown_increase = max(0.0, drawdown_after - drawdown_before)
    reward = pnl_component
    reward -= drawdown_increase * config.drawdown_penalty * config.pnl_scale
    if invalid_action:
        reward -= config.invalid_action_penalty
    if flipped:
        reward -= config.position_flip_penalty

    if not np.isfinite(reward):
        return 0.0
    return float(reward)
