"""Target-position trading environment.

Actions encode desired exposure instead of imperative trade commands:

    0 -> short
    1 -> flat
    2 -> long

The optional trend-follow mode constrains entries/exits to a trend filter so the
agent learns timing inside a trend-following strategy instead of overtrading.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import numpy as np
import pandas as pd

from .config import RLConfig
from .reward import compute_reward
from .bar_data import prepare_bars
from .state_builder import PositionState, StateBuilder


class TargetPosition(IntEnum):
    SHORT = 0
    FLAT = 1
    LONG = 2


TARGET_TO_POSITION = {
    TargetPosition.SHORT: -1,
    TargetPosition.FLAT: 0,
    TargetPosition.LONG: 1,
}


@dataclass
class TargetEnvState:
    step_index: int
    position: int
    entry_price: float | None
    equity: float
    peak_equity: float
    bars_in_position: int
    cooldown_remaining: int = 0
    last_trade_cost: float = 0.0
    turnover: float = 0.0


class TargetPositionEnv:
    """Single-symbol environment using target-position actions."""

    metadata = {"render_modes": []}

    def __init__(self, bars: pd.DataFrame, config: RLConfig | None = None):
        self.config = config or RLConfig()
        self.config.validate()
        self.bars = prepare_bars(bars)
        if len(self.bars) < 2:
            raise ValueError("TargetPositionEnv requires at least 2 bars")
        self.state_builder = StateBuilder(self.config.feature_columns)
        self.state = self._initial_state()

    @property
    def action_space_n(self) -> int:
        return len(TargetPosition)

    @property
    def observation_size(self) -> int:
        return self.state_builder.observation_size

    def reset(self, seed: int | None = None) -> tuple[np.ndarray, dict[str, Any]]:
        if seed is not None:
            np.random.default_rng(seed)
        self.state = self._initial_state()
        return self._observation(), self._info(done=False)

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        requested_target = TARGET_TO_POSITION[TargetPosition(int(action))]
        s = self.state
        row = self.bars.iloc[s.step_index]
        target, gate_reason = self._apply_trend_follow_rules(requested_target, row)
        forced_flat = False
        early_exit_blocked = gate_reason == "min_hold"

        if (
            self.config.max_position_bars is not None
            and s.position != 0
            and s.bars_in_position >= self.config.max_position_bars
        ):
            target = 0
            forced_flat = True
            gate_reason = "max_position_bars"

        equity_before = s.equity
        peak_equity_before = s.peak_equity
        position_before = s.position
        next_row = self.bars.iloc[s.step_index + 1]
        current_price = float(row["close"])
        next_open = float(next_row.get("open", next_row["close"]))
        next_price = float(next_row["close"])

        gap_return = self._position_return(position_before, current_price, next_open)
        s.equity *= max(0.0, 1.0 + gap_return)
        s.peak_equity = max(s.peak_equity, s.equity)

        turnover = abs(target - position_before) * self.config.position_fraction
        trade_cost = turnover * (self.config.fee_rate + self.config.slippage_rate)
        flipped = position_before != 0 and target != 0 and position_before != target

        if target != position_before:
            s.position = target
            s.bars_in_position = 0
            if target == 0:
                s.entry_price = None
                s.cooldown_remaining = self.config.cooldown_bars
            elif target == 1:
                s.entry_price = next_open * (1.0 + self.config.slippage_rate)
            else:
                s.entry_price = next_open * (1.0 - self.config.slippage_rate)

        intrabar_return = self._position_return(target, next_open, next_price)
        funding_rate = float(next_row.get("funding_rate", self.config.funding_rate_8h))
        funding_cost = (
            target
            * funding_rate
            * (self.config.bar_minutes / 480.0)
            * self.config.position_fraction
            if target != 0
            else 0.0
        )
        s.equity *= max(0.0, 1.0 + intrabar_return - trade_cost - funding_cost)
        s.peak_equity = max(s.peak_equity, s.equity)
        s.turnover += turnover

        if s.position == 0:
            s.bars_in_position = 0
            if s.cooldown_remaining > 0 and target == position_before:
                s.cooldown_remaining -= 1
        else:
            s.bars_in_position += 1

        s.step_index += 1
        terminated = s.step_index >= len(self.bars) - 1
        if self.config.max_episode_steps is not None:
            terminated = terminated or s.step_index >= self.config.max_episode_steps
        truncated = False

        terminal_liquidation = False
        if terminated and s.position != 0:
            liquidation_turnover = self.config.position_fraction
            liquidation_cost = liquidation_turnover * (
                self.config.fee_rate + self.config.slippage_rate
            )
            s.equity *= max(0.0, 1.0 - liquidation_cost)
            s.turnover += liquidation_turnover
            trade_cost += liquidation_cost
            s.position = 0
            s.entry_price = None
            s.bars_in_position = 0
            terminal_liquidation = True
        s.last_trade_cost = trade_cost
        s.peak_equity = max(s.peak_equity, s.equity)

        reward = compute_reward(
            equity_before=equity_before,
            equity_after=s.equity,
            peak_equity_before=peak_equity_before,
            invalid_action=False,
            flipped=flipped,
            config=self.config.reward,
        )
        reward -= turnover * self.config.reward.trade_action_penalty
        if self.config.reward.probability_shaping and self._unsupported_position(target, row):
            reward -= self.config.reward.unsupported_position_penalty
        if early_exit_blocked:
            reward -= self.config.reward.early_exit_penalty
        if target != 0:
            reward -= self.config.reward.position_hold_penalty
            if self.config.max_position_bars:
                exposure_age = min(1.0, s.bars_in_position / self.config.max_position_bars)
                reward -= self.config.reward.directional_exposure_penalty * exposure_age

        return self._observation(), reward, terminated, truncated, self._info(
            done=terminated,
            requested_target=requested_target,
            target_position=target,
            position_before=position_before,
            gross_return=gap_return + intrabar_return,
            gap_return=gap_return,
            intrabar_return=intrabar_return,
            turnover=turnover,
            funding_cost=funding_cost,
            execution_price=next_open,
            mark_price=next_price,
            terminal_liquidation=terminal_liquidation,
            forced_flat=forced_flat,
            gate_reason=gate_reason,
        )

    def _initial_state(self) -> TargetEnvState:
        return TargetEnvState(
            step_index=0,
            position=0,
            entry_price=None,
            equity=self.config.initial_equity,
            peak_equity=self.config.initial_equity,
            bars_in_position=0,
        )

    def _apply_trend_follow_rules(self, target: int, row: pd.Series) -> tuple[int, str | None]:
        if not self.config.trend_follow_mode:
            return target, None

        s = self.state
        long_allowed = self._trend_allowed(1, row)
        short_allowed = self._trend_allowed(-1, row)
        current_supported = s.position == 0 or self._trend_allowed(s.position, row)

        if s.position != 0:
            if self.config.force_exit_on_trend_break and not current_supported:
                return 0, "trend_break"
            if target == 0 and s.bars_in_position < self.config.min_position_bars and current_supported:
                return s.position, "min_hold"
            if target != 0 and target != s.position:
                return 0, "no_direct_flip"
            if target == 1 and not long_allowed:
                return s.position, "long_not_allowed"
            if target == -1 and not short_allowed:
                return s.position, "short_not_allowed"
            return target, None

        if s.cooldown_remaining > 0:
            return 0, "cooldown"
        if target == 1 and not long_allowed:
            return 0, "long_not_allowed"
        if target == -1 and not short_allowed:
            return 0, "short_not_allowed"
        return target, None

    def _trend_allowed(self, direction: int, row: pd.Series) -> bool:
        long_prob = float(row.get("long_prob", 0.5))
        short_prob = float(row.get("short_prob", 0.5))
        spread = long_prob - short_prob
        confidence = max(long_prob, short_prob)
        ema = float(row.get("ema_align_score", row.get("5m_ema_align_score", 0.0)))
        hurst = float(row.get("hurst", row.get("5m_hurst", 0.75)))
        vol = float(row.get("vol_regime", row.get("5m_vol_regime", 1.0)))
        monthly = float(row.get("monthly_sma_distance", 0.0))
        regime_ok = hurst >= self.config.trend_min_hurst and vol < self.config.trend_max_vol_regime
        if direction > 0:
            return (
                spread >= self.config.trend_min_gap
                and confidence >= self.config.trend_min_confidence
                and ema >= 0.0
                and monthly >= -self.config.trend_monthly_tolerance
                and regime_ok
            )
        return (
            spread <= -self.config.trend_min_gap
            and confidence >= self.config.trend_min_confidence
            and ema <= 0.0
            and monthly <= self.config.trend_monthly_tolerance
            and regime_ok
        )

    def _unsupported_position(self, target: int, row: pd.Series) -> bool:
        if target == 0:
            return False
        if self.config.trend_follow_mode:
            return not self._trend_allowed(target, row)
        long_prob = float(row.get("long_prob", 0.5))
        short_prob = float(row.get("short_prob", 0.5))
        threshold = self.config.reward.opportunity_threshold
        if target == 1:
            return not (long_prob >= threshold and long_prob > short_prob)
        return not (short_prob >= threshold and short_prob > long_prob)

    def _position_return(self, position: int, current_price: float, next_price: float) -> float:
        if position == 0 or current_price <= 0:
            return 0.0
        raw = (next_price / current_price) - 1.0
        return raw * position * self.config.position_fraction

    def _observation(self) -> np.ndarray:
        row = self.bars.iloc[self.state.step_index]
        return self.state_builder.build(row, self._position_state())

    def _position_state(self) -> PositionState:
        row = self.bars.iloc[self.state.step_index]
        close = float(row["close"])
        unrealized = 0.0
        if self.state.position != 0 and self.state.entry_price:
            unrealized = ((close / self.state.entry_price) - 1.0) * self.state.position
        drawdown = 0.0 if self.state.peak_equity <= 0 else max(0.0, 1.0 - self.state.equity / self.state.peak_equity)
        equity_return = self.state.equity / self.config.initial_equity - 1.0
        return PositionState(
            position=self.state.position,
            unrealized_return=unrealized,
            bars_in_position=self.state.bars_in_position,
            equity_return=equity_return,
            drawdown=drawdown,
        )

    def _info(self, *, done: bool, **extra: Any) -> dict[str, Any]:
        info = {
            "step_index": self.state.step_index,
            "done": done,
            "position": self.state.position,
            "equity": float(self.state.equity),
            "peak_equity": float(self.state.peak_equity),
            "last_trade_cost": float(self.state.last_trade_cost),
            "turnover": float(self.state.turnover),
            "cooldown_remaining": int(self.state.cooldown_remaining),
        }
        info.update(extra)
        return info


def target_action_from_position(position: int) -> int:
    if position < 0:
        return int(TargetPosition.SHORT)
    if position > 0:
        return int(TargetPosition.LONG)
    return int(TargetPosition.FLAT)
