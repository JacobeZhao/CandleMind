"""Gymnasium adapters for Stable-Baselines3 training."""

from __future__ import annotations

from typing import Any

import numpy as np

from .env import TradingEnv
from .policy import action_mask_for_signal
from .target_env import TargetPositionEnv

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError as exc:  # pragma: no cover
    gym = None
    spaces = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class SB3TradingEnv(gym.Env if gym is not None else object):
    """Stable-Baselines3 compatible wrapper around TradingEnv."""

    metadata = {"render_modes": []}

    def __init__(self, env: TradingEnv):
        if _IMPORT_ERROR is not None:
            raise ImportError("gymnasium is required for SB3TradingEnv") from _IMPORT_ERROR
        self.env = env
        self.action_space = spaces.Discrete(env.action_space_n)
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(env.observation_size,),
            dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        return self.env.reset(seed=seed)

    def step(self, action: int):
        return self.env.step(int(action))

    def action_masks(self):
        row = self.env.bars.iloc[self.env.state.step_index]
        return np.asarray(
            action_mask_for_signal(
                int(self.env.state.position),
                float(row.get("long_prob", 0.5)),
                float(row.get("short_prob", 0.5)),
                self.env.config.reward.opportunity_threshold,
            ),
            dtype=bool,
        )


class SB3TargetPositionEnv(gym.Env if gym is not None else object):
    """Stable-Baselines3 wrapper for target-position actions."""

    metadata = {"render_modes": []}

    def __init__(self, env: TargetPositionEnv):
        if _IMPORT_ERROR is not None:
            raise ImportError("gymnasium is required for SB3TargetPositionEnv") from _IMPORT_ERROR
        self.env = env
        self.action_space = spaces.Discrete(env.action_space_n)
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(env.observation_size,),
            dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        return self.env.reset(seed=seed)

    def step(self, action: int):
        return self.env.step(int(action))
