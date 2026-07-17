"""Configuration for the RL historical trading environment."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RewardConfig:
    pnl_scale: float = 100.0
    invalid_action_penalty: float = 0.05
    trade_cost_penalty_scale: float = 1.0
    drawdown_penalty: float = 0.10
    position_flip_penalty: float = 0.02
    opportunity_threshold: float = 0.62
    flat_opportunity_penalty: float = 0.01
    signal_entry_bonus: float = 0.02
    trade_action_penalty: float = 0.01
    unsupported_position_penalty: float = 0.02
    position_hold_penalty: float = 0.0
    directional_exposure_penalty: float = 0.0
    early_exit_penalty: float = 0.02
    probability_shaping: bool = True


@dataclass(frozen=True)
class RLConfig:
    initial_equity: float = 1.0
    position_fraction: float = 1.0
    fee_rate: float = 0.0010
    slippage_rate: float = 0.0002
    funding_rate_8h: float = 0.0001
    bar_minutes: int = 5
    max_episode_steps: int | None = None
    max_position_bars: int | None = 288
    min_position_bars: int = 0
    cooldown_bars: int = 0
    trend_follow_mode: bool = False
    force_exit_on_trend_break: bool = True
    trend_min_gap: float = 0.06
    trend_min_confidence: float = 0.52
    trend_min_hurst: float = 0.50
    trend_max_vol_regime: float = 2.0
    trend_monthly_tolerance: float = 0.03
    feature_columns: tuple[str, ...] = field(default_factory=tuple)
    reward: RewardConfig = field(default_factory=RewardConfig)

    def validate(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if not 0 < self.position_fraction <= 1:
            raise ValueError("position_fraction must be in (0, 1]")
        if self.fee_rate < 0 or self.slippage_rate < 0:
            raise ValueError("fees and slippage must be non-negative")
        if not math.isfinite(self.funding_rate_8h):
            raise ValueError("funding_rate_8h must be finite")
        if self.bar_minutes <= 0:
            raise ValueError("bar_minutes must be positive")
        if self.max_episode_steps is not None and self.max_episode_steps <= 0:
            raise ValueError("max_episode_steps must be positive when set")
        if self.max_position_bars is not None and self.max_position_bars <= 0:
            raise ValueError("max_position_bars must be positive when set")
        if self.min_position_bars < 0:
            raise ValueError("min_position_bars must be non-negative")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be non-negative")
        if self.trend_min_gap < 0:
            raise ValueError("trend_min_gap must be non-negative")
        if self.trend_min_confidence < 0:
            raise ValueError("trend_min_confidence must be non-negative")
