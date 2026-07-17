"""Model loading and prediction helpers for RL policies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .policy import action_mask_for_signal, sanitize_action


def load_policy_model(path: str | Path) -> Any:
    """Load MaskablePPO models first, then fall back to plain PPO."""
    try:
        from sb3_contrib import MaskablePPO
        return MaskablePPO.load(path)
    except Exception:
        from stable_baselines3 import PPO
        return PPO.load(path)


def predict_action(model: Any, obs, info: dict, *, sanitize: bool = True, use_mask: bool = True) -> int:
    """Predict an action with optional action mask and execution sanitization."""
    position = int(info.get("position", 0))
    if use_mask:
        try:
            action, _ = model.predict(obs, deterministic=True, action_masks=np.asarray(action_mask_for_signal(position, float(obs[0]), float(obs[1])), dtype=bool))
        except TypeError:
            action, _ = model.predict(obs, deterministic=True)
    else:
        action, _ = model.predict(obs, deterministic=True)
    action_int = int(action)
    if sanitize:
        return sanitize_action(action_int, position)
    return action_int
