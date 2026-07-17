"""Evaluation helpers for RL trading policies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .actions import Action
from .config import RLConfig
from .env import TradingEnv

Policy = Callable[[np.ndarray, dict[str, Any]], int]


@dataclass(frozen=True)
class EvaluationResult:
    steps: int
    final_equity: float
    total_reward: float
    max_drawdown: float
    invalid_actions: int
    action_counts: dict[str, int]


def evaluate_policy(bars: pd.DataFrame, policy: Policy, config: RLConfig | None = None) -> EvaluationResult:
    env = TradingEnv(bars, config=config)
    obs, info = env.reset(seed=0)
    total_reward = 0.0
    invalid_actions = 0
    max_drawdown = 0.0
    action_counts = {a.name.lower(): 0 for a in Action}
    steps = 0

    while True:
        action = int(policy(obs, info))
        action_counts[Action(action).name.lower()] += 1
        obs, reward, terminated, truncated, info = env.step(action)
        total_reward += reward
        invalid_actions += int(bool(info.get("invalid_action")))
        peak = float(info["peak_equity"])
        equity = float(info["equity"])
        drawdown = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        max_drawdown = max(max_drawdown, drawdown)
        steps += 1
        if terminated or truncated:
            break

    return EvaluationResult(
        steps=steps,
        final_equity=float(info["equity"]),
        total_reward=float(total_reward),
        max_drawdown=float(max_drawdown),
        invalid_actions=invalid_actions,
        action_counts=action_counts,
    )


def threshold_policy(long_threshold: float = 0.62, short_threshold: float = 0.62) -> Policy:
    """Simple baseline policy that reacts to long_prob/short_prob in the observation."""

    def _policy(obs: np.ndarray, info: dict[str, Any]) -> int:
        long_prob = float(obs[0])
        short_prob = float(obs[1])
        position = int(info.get("position", 0))
        if position == 0:
            if long_prob >= long_threshold and long_prob > short_prob:
                return Action.OPEN_LONG
            if short_prob >= short_threshold and short_prob > long_prob:
                return Action.OPEN_SHORT
            return Action.HOLD
        if position == 1 and short_prob >= short_threshold:
            return Action.CLOSE
        if position == -1 and long_prob >= long_threshold:
            return Action.CLOSE
        return Action.HOLD

    return _policy


def make_synthetic_bars(rows: int = 240) -> pd.DataFrame:
    t = np.arange(rows, dtype=np.float64)
    close = 100.0 + np.sin(t / 12.0) * 2.0 + t * 0.02
    trend = np.gradient(close)
    long_prob = np.clip(0.50 + trend * 2.0, 0.05, 0.95)
    short_prob = np.clip(0.50 - trend * 2.0, 0.05, 0.95)
    return pd.DataFrame(
        {
            "open_time": np.arange(rows, dtype=np.int64) * 300_000,
            "close": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "volume": 1000 + np.cos(t / 7.0) * 100,
            "long_prob": long_prob,
            "short_prob": short_prob,
        }
    )


def smoke_test() -> EvaluationResult:
    return evaluate_policy(make_synthetic_bars(), threshold_policy(0.58, 0.58))


if __name__ == "__main__":
    result = smoke_test()
    print(result)
