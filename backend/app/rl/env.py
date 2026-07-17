"""Minimal Gym-like trading environment for the RL decision layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .actions import Action, action_name
from .config import RLConfig
from .reward import compute_reward
from .bar_data import prepare_bars
from .state_builder import PositionState, StateBuilder


@dataclass
class EnvState:
    step_index: int
    position: int
    entry_price: float | None
    equity: float
    peak_equity: float
    bars_in_position: int
    last_trade_cost: float = 0.0


class TradingEnv:
    """Historical single-symbol, single-position environment.

    The environment is intentionally dependency-light: it follows the common
    reset/step pattern without requiring gymnasium or stable-baselines3.
    """

    metadata = {"render_modes": []}

    def __init__(self, bars: pd.DataFrame, config: RLConfig | None = None):
        self.config = config or RLConfig()
        self.config.validate()
        self.bars = prepare_bars(bars)
        if len(self.bars) < 2:
            raise ValueError("TradingEnv requires at least 2 bars")
        self.state_builder = StateBuilder(self.config.feature_columns)
        self.state = EnvState(
            step_index=0,
            position=0,
            entry_price=None,
            equity=self.config.initial_equity,
            peak_equity=self.config.initial_equity,
            bars_in_position=0,
        )

    @property
    def action_space_n(self) -> int:
        return len(Action)

    @property
    def observation_size(self) -> int:
        return self.state_builder.observation_size

    def reset(self, seed: int | None = None) -> tuple[np.ndarray, dict[str, Any]]:
        if seed is not None:
            np.random.default_rng(seed)
        self.state = EnvState(
            step_index=0,
            position=0,
            entry_price=None,
            equity=self.config.initial_equity,
            peak_equity=self.config.initial_equity,
            bars_in_position=0,
        )
        return self._observation(), self._info(done=False)

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        action_enum = Action(int(action))
        s = self.state
        equity_before = s.equity
        peak_equity_before = s.peak_equity
        row = self.bars.iloc[s.step_index]
        next_row = self.bars.iloc[s.step_index + 1]
        current_price = float(row["close"])
        next_open = float(next_row.get("open", next_row["close"]))
        next_price = float(next_row["close"])
        position_before = s.position

        invalid_action = False
        target = position_before

        if action_enum == Action.OPEN_LONG:
            if s.position == 1:
                invalid_action = True
            else:
                target = 1
        elif action_enum == Action.OPEN_SHORT:
            if s.position == -1:
                invalid_action = True
            else:
                target = -1
        elif action_enum == Action.CLOSE:
            if s.position == 0:
                invalid_action = True
            else:
                target = 0

        flipped = position_before != 0 and target != 0 and position_before != target
        gap_return = self._position_return_for(position_before, current_price, next_open)
        s.equity *= max(0.0, 1.0 + gap_return)
        s.peak_equity = max(s.peak_equity, s.equity)

        turnover = abs(target - position_before) * self.config.position_fraction
        trade_cost = turnover * (self.config.fee_rate + self.config.slippage_rate)
        if target != position_before:
            s.position = target
            s.bars_in_position = 0
            if target == 0:
                s.entry_price = None
            elif target > 0:
                s.entry_price = next_open * (1.0 + self.config.slippage_rate)
            else:
                s.entry_price = next_open * (1.0 - self.config.slippage_rate)

        intrabar_return = self._position_return_for(target, next_open, next_price)
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

        if s.position == 0:
            s.bars_in_position = 0
        else:
            s.bars_in_position += 1

        s.step_index += 1
        terminated = s.step_index >= len(self.bars) - 1
        if self.config.max_episode_steps is not None:
            terminated = terminated or s.step_index >= self.config.max_episode_steps
        truncated = False

        terminal_liquidation = False
        if terminated and s.position != 0:
            liquidation_cost = self.config.position_fraction * (
                self.config.fee_rate + self.config.slippage_rate
            )
            s.equity *= max(0.0, 1.0 - liquidation_cost)
            trade_cost += liquidation_cost
            s.position = 0
            s.entry_price = None
            s.bars_in_position = 0
            terminal_liquidation = True
        s.last_trade_cost = trade_cost

        reward = compute_reward(
            equity_before=equity_before,
            equity_after=s.equity,
            peak_equity_before=peak_equity_before,
            invalid_action=invalid_action,
            flipped=flipped,
            config=self.config.reward,
        )
        if self.config.reward.probability_shaping:
            reward += self._signal_shaping(action_enum, row, position_before)
        if not invalid_action and action_enum != Action.HOLD:
            reward -= self.config.reward.trade_action_penalty
        return self._observation(), reward, terminated, truncated, self._info(
            done=terminated,
            action=action_name(action_enum),
            invalid_action=invalid_action,
            gross_return=gap_return + intrabar_return,
            gap_return=gap_return,
            intrabar_return=intrabar_return,
            funding_cost=funding_cost,
            execution_price=next_open,
            mark_price=next_price,
            terminal_liquidation=terminal_liquidation,
        )

    def _signal_shaping(self, action: Action, row: pd.Series, position_before: int) -> float:
        long_prob = float(row.get("long_prob", 0.5))
        short_prob = float(row.get("short_prob", 0.5))
        threshold = self.config.reward.opportunity_threshold
        strong_long = long_prob >= threshold and long_prob > short_prob
        strong_short = short_prob >= threshold and short_prob > long_prob
        if position_before == 0 and action == Action.HOLD and (strong_long or strong_short):
            return -self.config.reward.flat_opportunity_penalty
        if position_before == 0 and action == Action.OPEN_LONG and strong_long:
            return self.config.reward.signal_entry_bonus
        if position_before == 0 and action == Action.OPEN_SHORT and strong_short:
            return self.config.reward.signal_entry_bonus
        return 0.0

    def _transaction_cost(self, price: float, *, close_existing: bool) -> float:
        turns = 2 if close_existing else 1
        return turns * (self.config.fee_rate + self.config.slippage_rate) * self.config.position_fraction

    def _position_return(self, current_price: float, next_price: float) -> float:
        return self._position_return_for(self.state.position, current_price, next_price)

    def _position_return_for(self, position: int, current_price: float, next_price: float) -> float:
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
        }
        info.update(extra)
        return info
