"""Discrete actions used by the RL trading overlay."""

from __future__ import annotations

from enum import IntEnum


class Action(IntEnum):
    HOLD = 0
    OPEN_LONG = 1
    OPEN_SHORT = 2
    CLOSE = 3


def target_position(action: int, current_position: int) -> int:
    """Return target position for an action, preserving position on hold/close."""
    action = Action(int(action))
    if action == Action.OPEN_LONG:
        return 1
    if action == Action.OPEN_SHORT:
        return -1
    if action in (Action.HOLD, Action.CLOSE):
        return current_position
    raise ValueError(f"Unsupported action: {action}")


def action_name(action: int) -> str:
    return Action(int(action)).name.lower()
