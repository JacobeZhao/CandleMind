"""Policy helpers for safe RL action execution."""

from __future__ import annotations

from .actions import Action


def sanitize_action(action: int, position: int) -> int:
    """Convert invalid trade actions to HOLD for execution/evaluation safety."""
    action_enum = Action(int(action))
    if action_enum == Action.OPEN_LONG and position == 1:
        return int(Action.HOLD)
    if action_enum == Action.OPEN_SHORT and position == -1:
        return int(Action.HOLD)
    if action_enum == Action.CLOSE and position == 0:
        return int(Action.HOLD)
    return int(action_enum)


def valid_actions(position: int) -> list[int]:
    """Return executable actions for a position state, ignoring signal gates."""
    if position == 0:
        return [int(Action.HOLD), int(Action.OPEN_LONG), int(Action.OPEN_SHORT)]
    if position == 1:
        return [int(Action.HOLD), int(Action.OPEN_SHORT), int(Action.CLOSE)]
    if position == -1:
        return [int(Action.HOLD), int(Action.OPEN_LONG), int(Action.CLOSE)]
    raise ValueError(f"Unsupported position: {position}")


def valid_actions_for_signal(position: int, long_prob: float, short_prob: float, threshold: float = 0.62) -> list[int]:
    """Return actions allowed by both position state and ML signal strength."""
    strong_long = long_prob >= threshold and long_prob > short_prob
    strong_short = short_prob >= threshold and short_prob > long_prob
    if position == 0:
        actions = [int(Action.HOLD)]
        if strong_long:
            actions.append(int(Action.OPEN_LONG))
        if strong_short:
            actions.append(int(Action.OPEN_SHORT))
        return actions
    if position == 1:
        actions = [int(Action.HOLD), int(Action.CLOSE)]
        if strong_short:
            actions.append(int(Action.OPEN_SHORT))
        return actions
    if position == -1:
        actions = [int(Action.HOLD), int(Action.CLOSE)]
        if strong_long:
            actions.append(int(Action.OPEN_LONG))
        return actions
    raise ValueError(f"Unsupported position: {position}")


def action_mask(position: int) -> list[bool]:
    """Return a boolean mask ordered by Action enum value, ignoring signal gates."""
    allowed = set(valid_actions(position))
    return [int(action) in allowed for action in Action]


def action_mask_for_signal(position: int, long_prob: float, short_prob: float, threshold: float = 0.62) -> list[bool]:
    """Return a boolean action mask with signal-gated entries/reversals."""
    allowed = set(valid_actions_for_signal(position, long_prob, short_prob, threshold))
    return [int(action) in allowed for action in Action]
