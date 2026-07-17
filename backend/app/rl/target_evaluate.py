"""Evaluation helpers for target-position policies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from .config import RLConfig
from .evaluate import EvaluationResult, make_synthetic_bars
from .metrics import DetailedEvaluation, Trade, compute_trade_stats
from .target_env import TargetPosition, TargetPositionEnv

TargetPolicy = Callable[[np.ndarray, dict[str, Any]], int]


def evaluate_target_policy(
    bars: pd.DataFrame,
    policy: TargetPolicy,
    config: RLConfig | None = None,
) -> EvaluationResult:
    return evaluate_target_policy_detailed(bars, policy, config=config).summary


def evaluate_target_policy_detailed(
    bars: pd.DataFrame,
    policy: TargetPolicy,
    config: RLConfig | None = None,
) -> DetailedEvaluation:
    env = TargetPositionEnv(bars, config=config)
    obs, info = env.reset(seed=0)
    total_reward = 0.0
    max_drawdown = 0.0
    counts = {a.name.lower(): 0 for a in TargetPosition}
    steps = 0
    open_trade: dict[str, Any] | None = None
    trades: list[Trade] = []

    while True:
        position_before = int(info.get("position", 0))
        step_before = int(info.get("step_index", 0))
        action = TargetPosition(int(policy(obs, info)))
        counts[action.name.lower()] += 1
        obs, reward, terminated, truncated, info = env.step(int(action))
        position_after = int(info.get("position", 0))
        execution_price = float(info.get("execution_price", env.bars.iloc[step_before]["close"]))
        exit_price = (
            float(info.get("mark_price", execution_price))
            if info.get("terminal_liquidation")
            else execution_price
        )

        if position_before == 0 and position_after != 0:
            open_trade = {
                "entry_step": step_before + 1,
                "direction": position_after,
                "entry_price": execution_price,
            }
        elif position_before != 0 and position_after == 0 and open_trade is not None:
            trades.append(_close_trade(open_trade, step_before + 1, exit_price))
            open_trade = None
        elif position_before != 0 and position_after != 0 and position_before != position_after:
            if open_trade is not None:
                trades.append(_close_trade(open_trade, step_before + 1, execution_price))
            open_trade = {
                "entry_step": step_before + 1,
                "direction": position_after,
                "entry_price": execution_price,
            }

        total_reward += reward
        peak = float(info["peak_equity"])
        equity = float(info["equity"])
        drawdown = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        max_drawdown = max(max_drawdown, drawdown)
        steps += 1
        if terminated or truncated:
            if open_trade is not None:
                last_step = int(info.get("step_index", step_before))
                last_price = float(env.bars.iloc[last_step]["close"])
                trades.append(_close_trade(open_trade, last_step, last_price))
            break

    summary = EvaluationResult(
        steps=steps,
        final_equity=float(info["equity"]),
        total_reward=float(total_reward),
        max_drawdown=float(max_drawdown),
        invalid_actions=0,
        action_counts=counts,
    )
    return DetailedEvaluation(summary=summary, trade_stats=compute_trade_stats(trades))


def threshold_target_policy(long_threshold: float = 0.62, short_threshold: float = 0.62) -> TargetPolicy:
    def _policy(obs: np.ndarray, info: dict[str, Any]) -> int:
        long_prob = float(obs[0])
        short_prob = float(obs[1])
        position = int(info.get("position", 0))
        if long_prob >= long_threshold and long_prob > short_prob:
            return int(TargetPosition.LONG)
        if short_prob >= short_threshold and short_prob > long_prob:
            return int(TargetPosition.SHORT)
        if position != 0:
            return int(TargetPosition.FLAT)
        return int(TargetPosition.FLAT)

    return _policy


def _close_trade(open_trade: dict[str, Any], exit_step: int, exit_price: float) -> Trade:
    direction = int(open_trade["direction"])
    entry_step = int(open_trade["entry_step"])
    entry_price = float(open_trade["entry_price"])
    ret = 0.0 if entry_price <= 0 else ((exit_price / entry_price) - 1.0) * direction
    return Trade(
        entry_step=entry_step,
        exit_step=exit_step,
        direction=direction,
        entry_price=entry_price,
        exit_price=float(exit_price),
        return_pct=float(ret),
        bars_held=max(0, exit_step - entry_step),
    )


def smoke_test() -> EvaluationResult:
    return evaluate_target_policy(make_synthetic_bars(), threshold_target_policy(0.58, 0.58))


if __name__ == "__main__":
    print(smoke_test())
