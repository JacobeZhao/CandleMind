"""Pure signed EMA trend lifecycle transitions.

The lifecycle consumes one causal, completed-bar observation at a time.  It
does not place orders or inspect an execution engine; callers own fills and
feed the resulting position R multiple back through the configured column.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Any, Mapping

import pandas as pd


class LifecyclePhase(str, Enum):
    UNARMED = "unarmed"
    LONG_SETUP = "long_setup"
    SHORT_SETUP = "short_setup"
    LONG_PULLBACK = "long_pullback"
    SHORT_PULLBACK = "short_pullback"
    LONG_ENTERED = "long_entered"
    SHORT_ENTERED = "short_entered"
    LONG_CONSUMED = "long_consumed"
    SHORT_CONSUMED = "short_consumed"
    LONG_REARMING = "long_rearming"
    SHORT_REARMING = "short_rearming"


class LifecycleIntent(str, Enum):
    NONE = "none"
    ACCEPT_ENTRY = "accept_entry"
    HOLD = "hold"
    EXIT = "exit"


class StopPhase(str, Enum):
    NONE = "none"
    STRUCTURAL_INITIAL = "structural_initial"
    EMA15_55 = "15m_ema55"
    EMA1H_21 = "1h_ema21"


@dataclass(frozen=True)
class EmaLifecycleColumns:
    decision_at: str = "decision_available_at"
    completed_15m_at: str = "15m_ema_available_at"
    completed_1h_at: str = "1h_ema_available_at"
    completed_4h_at: str = "4h_ema_available_at"
    alignment_1d: str = "1d_ema_alignment"
    alignment_4h: str = "4h_ema_alignment"
    persistence_4h: str = "4h_ema_alignment_persistence"
    slope_1d: str = "1d_ema200_slope3"
    slope_4h: str = "4h_ema55_slope3"
    distance_15m_ema55: str = "15m_close_ema55_log"
    slope_15m: str = "15m_ema55_slope3"
    slope_5m: str = "5m_ema55_slope3"
    distance_1h_ema21: str = "1h_close_ema21_log"
    position_r: str = "position_r"


@dataclass(frozen=True)
class EmaLifecycleConfig:
    columns: EmaLifecycleColumns = EmaLifecycleColumns()
    entry_mode: str = "pullback_recross"
    full_alignment: float = 1.0
    minimum_persistence_4h_bars: int = 8
    minimum_stop_fraction: float = 0.006
    maximum_stop_fraction: float = 0.025
    persistent_1h_breaks: int = 2
    rearm_4h_breaks: int = 2
    local_debounce_window_15m: int = 3
    local_debounce_required_15m: int = 2
    local_reset_required_15m: int = 2

    def validate(self) -> None:
        if self.entry_mode not in {
            "pullback_recross", "direct_confirmation", "local_confirmation",
            "transition_expansion", "expansion_pullback_recross",
        }:
            raise ValueError(
                "entry_mode must be pullback_recross, direct_confirmation, "
                "local_confirmation, transition_expansion, or "
                "expansion_pullback_recross"
            )
        names = tuple(vars(self.columns).values())
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("lifecycle column names must be non-empty strings")
        if len(set(names)) != len(names):
            raise ValueError("lifecycle column names must be unique")
        if not 0.0 < self.full_alignment <= 1.0:
            raise ValueError("full_alignment must be in (0, 1]")
        if (
            isinstance(self.minimum_persistence_4h_bars, bool)
            or not isinstance(self.minimum_persistence_4h_bars, int)
            or self.minimum_persistence_4h_bars < 1
        ):
            raise ValueError("minimum_persistence_4h_bars must be a positive integer")
        if not 0.0 < self.minimum_stop_fraction <= self.maximum_stop_fraction < 1.0:
            raise ValueError("stop fractions must satisfy 0 < minimum <= maximum < 1")
        if self.persistent_1h_breaks < 1:
            raise ValueError("persistent_1h_breaks must be positive")
        if self.rearm_4h_breaks < 2:
            raise ValueError("rearm_4h_breaks must be at least two")
        if (
            isinstance(self.local_debounce_window_15m, bool)
            or self.local_debounce_window_15m < 1
        ):
            raise ValueError("local_debounce_window_15m must be positive")
        if (
            isinstance(self.local_debounce_required_15m, bool)
            or not 1
            <= self.local_debounce_required_15m
            <= self.local_debounce_window_15m
        ):
            raise ValueError("local debounce requirement must fit its window")
        if (
            isinstance(self.local_reset_required_15m, bool)
            or self.local_reset_required_15m < 1
        ):
            raise ValueError("local_reset_required_15m must be positive")


@dataclass(frozen=True)
class EmaLifecycleState:
    phase: LifecyclePhase = LifecyclePhase.UNARMED
    last_decision_at: pd.Timestamp | None = None
    last_15m_at: pd.Timestamp | None = None
    last_1h_at: pd.Timestamp | None = None
    last_4h_at: pd.Timestamp | None = None
    last_4h_alignment: float | None = None
    last_4h_persistence: float | None = None
    pullback_15m_at: pd.Timestamp | None = None
    one_hour_break_count: int = 0
    rearm_break_count: int = 0
    structural_stop_fraction: float | None = None
    attained_stop_phase: StopPhase = StopPhase.NONE
    entry_pending: bool = False
    local_long_history: tuple[bool, ...] = ()
    local_short_history: tuple[bool, ...] = ()
    local_false_count: int = 0

    @property
    def direction(self) -> int:
        if self.phase.name.startswith("LONG_"):
            return 1
        if self.phase.name.startswith("SHORT_"):
            return -1
        return 0


@dataclass(frozen=True)
class EmaLifecycleTransition:
    state: EmaLifecycleState
    intent: LifecycleIntent
    reason: str
    direction: int
    accept_entry: bool
    hold: bool
    exit: bool
    structural_stop_fraction: float | None
    structural_stop_eligible: bool
    stop_phase: StopPhase
    trailing_enabled: bool


_SETUP = {1: LifecyclePhase.LONG_SETUP, -1: LifecyclePhase.SHORT_SETUP}
_PULLBACK = {1: LifecyclePhase.LONG_PULLBACK, -1: LifecyclePhase.SHORT_PULLBACK}
_ENTERED = {1: LifecyclePhase.LONG_ENTERED, -1: LifecyclePhase.SHORT_ENTERED}
_CONSUMED = {1: LifecyclePhase.LONG_CONSUMED, -1: LifecyclePhase.SHORT_CONSUMED}
_REARMING = {1: LifecyclePhase.LONG_REARMING, -1: LifecyclePhase.SHORT_REARMING}


def initial_ema_lifecycle_state() -> EmaLifecycleState:
    return EmaLifecycleState()


def force_exit_ema_lifecycle(
    state: EmaLifecycleState,
    *,
    reason: str,
) -> EmaLifecycleTransition:
    """Consume the incumbent trend epoch after an execution-owned forced exit."""

    if not isinstance(state, EmaLifecycleState):
        raise TypeError("state must be EmaLifecycleState")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("forced exit reason must be a non-empty string")
    if state.phase not in (
        LifecyclePhase.LONG_ENTERED,
        LifecyclePhase.SHORT_ENTERED,
    ) or state.entry_pending:
        raise ValueError("forced exit requires an entered lifecycle")
    direction = state.direction
    consumed = replace(state, phase=_CONSUMED[direction])
    return _result(
        consumed,
        intent=LifecycleIntent.EXIT,
        reason=reason,
        stop_fraction=state.structural_stop_fraction,
        stop_eligible=True,
        stop_phase=state.attained_stop_phase,
    )


def cancel_ema_lifecycle_entry(
    state: EmaLifecycleState,
    *,
    reason: str,
) -> EmaLifecycleTransition:
    """Return an unfilled accepted entry to setup without consuming its epoch."""

    if not isinstance(state, EmaLifecycleState):
        raise TypeError("state must be EmaLifecycleState")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("entry cancellation reason must be a non-empty string")
    if state.phase not in (
        LifecyclePhase.LONG_ENTERED,
        LifecyclePhase.SHORT_ENTERED,
    ) or not state.entry_pending:
        raise ValueError("entry cancellation requires a pending entry")
    direction = state.direction
    setup = replace(
        state,
        phase=_SETUP[direction],
        pullback_15m_at=None,
        one_hour_break_count=0,
        structural_stop_fraction=None,
        attained_stop_phase=StopPhase.NONE,
        entry_pending=False,
    )
    return _result(setup, reason=reason)


def confirm_ema_lifecycle_entry(state: EmaLifecycleState) -> EmaLifecycleState:
    """Confirm that execution filled the lifecycle's accepted entry."""

    if not isinstance(state, EmaLifecycleState):
        raise TypeError("state must be EmaLifecycleState")
    if state.phase not in (
        LifecyclePhase.LONG_ENTERED,
        LifecyclePhase.SHORT_ENTERED,
    ) or not state.entry_pending:
        raise ValueError("entry confirmation requires a pending entry")
    return replace(state, entry_pending=False)


def transition_ema_lifecycle(
    state: EmaLifecycleState,
    observation: Mapping[str, Any],
    config: EmaLifecycleConfig | None = None,
) -> EmaLifecycleTransition:
    """Advance the lifecycle from one completed 5m decision observation."""

    config = config or EmaLifecycleConfig()
    config.validate()
    values = _validated_observation(state, observation, config)
    decision_at, at_15m, at_1h, at_4h = values["timestamps"]
    numeric = values["numeric"]
    new_15m = state.last_15m_at is None or at_15m > state.last_15m_at
    new_1h = state.last_1h_at is None or at_1h > state.last_1h_at
    new_4h = state.last_4h_at is None or at_4h > state.last_4h_at
    current = replace(
        state,
        last_decision_at=decision_at,
        last_15m_at=at_15m,
        last_1h_at=at_1h,
        last_4h_at=at_4h,
        last_4h_alignment=numeric[config.columns.alignment_4h],
        last_4h_persistence=numeric[config.columns.persistence_4h],
    )

    if config.entry_mode == "local_confirmation":
        return _transition_local_confirmation(
            state,
            current,
            observation=observation,
            numeric=numeric,
            config=config,
            new_15m=new_15m,
        )
    if config.entry_mode in {"transition_expansion", "expansion_pullback_recross"}:
        return _transition_expansion(
            state,
            current,
            observation=observation,
            numeric=numeric,
            config=config,
            new_15m=new_15m,
        )

    if state.phase is LifecyclePhase.UNARMED:
        direction = _slow_epoch_direction(numeric, config)
        if direction:
            return _result(replace(current, phase=_SETUP[direction]), reason="slow_epoch_setup")
        return _result(current, reason="no_persistent_slow_epoch")

    direction = state.direction
    if state.phase in (_CONSUMED[direction], _REARMING[direction]):
        return _advance_rearm(current, numeric, config, direction, new_4h)

    if state.phase in (_SETUP[direction], _PULLBACK[direction]):
        if not _slow_geometry(numeric, config, direction):
            return _result(
                replace(current, phase=LifecyclePhase.UNARMED, pullback_15m_at=None),
                reason="slow_geometry_lost_before_entry",
            )
        distance = direction * numeric[config.columns.distance_15m_ema55]
        if state.phase is _SETUP[direction]:
            if config.entry_mode == "direct_confirmation":
                confirmed = (
                    new_15m
                    and distance > 0.0
                    and direction * numeric[config.columns.slope_5m] > 0.0
                    and direction * numeric[config.columns.slope_15m] > 0.0
                )
                if not confirmed:
                    return _result(current, reason="awaiting_direct_confirmation")
                return _entry_transition(
                    current,
                    direction=direction,
                    distance=distance,
                    config=config,
                    reason="direct_ema_confirmation",
                )
            if new_15m and distance <= 0.0:
                return _result(
                    replace(current, phase=_PULLBACK[direction], pullback_15m_at=at_15m),
                    reason="completed_15m_pullback",
                )
            return _result(current, reason="awaiting_completed_15m_pullback")

        recross = (
            new_15m
            and state.pullback_15m_at is not None
            and at_15m > state.pullback_15m_at
            and distance > 0.0
            and direction * numeric[config.columns.slope_5m] > 0.0
            and direction * numeric[config.columns.slope_15m] > 0.0
        )
        if not recross:
            return _result(current, reason="awaiting_positive_slope_recross")
        return _entry_transition(
            current,
            direction=direction,
            distance=distance,
            config=config,
            reason="completed_pullback_recross",
        )

    if state.phase is _ENTERED[direction]:
        if state.entry_pending:
            raise ValueError("pending EMA entry requires execution confirmation")
        r_multiple = numeric[config.columns.position_r]
        stop_phase = _later_stop_phase(
            state.attained_stop_phase, _stop_phase(r_multiple)
        )
        adverse_4h = new_4h and direction * numeric[config.columns.slope_4h] < 0.0
        broken_1h = direction * numeric[config.columns.distance_1h_ema21] <= 0.0
        break_count = state.one_hour_break_count
        if new_1h:
            break_count = break_count + 1 if broken_1h else 0
        current = replace(
            current,
            one_hour_break_count=break_count,
            attained_stop_phase=stop_phase,
        )
        if adverse_4h or break_count >= config.persistent_1h_breaks:
            reason = "adverse_4h_ema55_slope" if adverse_4h else "persistent_1h_ema21_break"
            return _result(
                replace(current, phase=_CONSUMED[direction]),
                intent=LifecycleIntent.EXIT,
                reason=reason,
                stop_fraction=state.structural_stop_fraction,
                stop_eligible=True,
                stop_phase=stop_phase,
            )
        return _result(
            current,
            intent=LifecycleIntent.HOLD,
            reason="position_hold",
            stop_fraction=state.structural_stop_fraction,
            stop_eligible=True,
            stop_phase=stop_phase,
        )

    raise ValueError(f"unsupported lifecycle phase: {state.phase!r}")


def _transition_local_confirmation(
    previous: EmaLifecycleState,
    current: EmaLifecycleState,
    *,
    observation: Mapping[str, Any],
    numeric: dict[str, float],
    config: EmaLifecycleConfig,
    new_15m: bool,
) -> EmaLifecycleTransition:
    try:
        distance_ema21 = float(observation["15m_close_to_ema21_log"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "local confirmation requires finite 15m_close_to_ema21_log"
        ) from exc
    if not math.isfinite(distance_ema21):
        raise ValueError(
            "local confirmation requires finite 15m_close_to_ema21_log"
        )

    raw = {
        direction: (
            direction * distance_ema21 > 0.0
            and direction * numeric[config.columns.slope_15m] > 0.0
            and direction * numeric[config.columns.slope_5m] > 0.0
        )
        for direction in (1, -1)
    }
    if new_15m:
        current = replace(
            current,
            local_long_history=_append_local_history(
                previous.local_long_history, raw[1], config
            ),
            local_short_history=_append_local_history(
                previous.local_short_history, raw[-1], config
            ),
        )

    confirmed = {
        1: sum(current.local_long_history) >= config.local_debounce_required_15m,
        -1: sum(current.local_short_history) >= config.local_debounce_required_15m,
    }
    if previous.phase in (LifecyclePhase.LONG_ENTERED, LifecyclePhase.SHORT_ENTERED):
        if previous.entry_pending:
            raise ValueError("pending EMA entry requires execution confirmation")
        direction = previous.direction
        false_count = previous.local_false_count
        if new_15m:
            false_count = 0 if raw[direction] else false_count + 1
        stop_phase = _later_stop_phase(
            previous.attained_stop_phase,
            _stop_phase(numeric[config.columns.position_r]),
        )
        current = replace(
            current,
            local_false_count=false_count,
            attained_stop_phase=stop_phase,
        )
        if false_count >= config.local_reset_required_15m or confirmed[-direction]:
            reason = (
                "opposite_local_confirmation"
                if confirmed[-direction]
                else "local_confirmation_lost"
            )
            return _result(
                replace(current, phase=_CONSUMED[direction]),
                intent=LifecycleIntent.EXIT,
                reason=reason,
                stop_fraction=previous.structural_stop_fraction,
                stop_eligible=True,
                stop_phase=stop_phase,
            )
        return _result(
            current,
            intent=LifecycleIntent.HOLD,
            reason="local_position_hold",
            stop_fraction=previous.structural_stop_fraction,
            stop_eligible=True,
            stop_phase=stop_phase,
        )

    if previous.phase is not LifecyclePhase.UNARMED:
        direction = previous.direction
        false_count = previous.local_false_count
        if new_15m:
            false_count = 0 if raw[direction] else false_count + 1
        if false_count < config.local_reset_required_15m:
            return _result(
                replace(current, local_false_count=false_count),
                reason="local_confirmation_cooldown",
            )
        return _result(
            replace(
                current,
                phase=LifecyclePhase.UNARMED,
                local_long_history=(),
                local_short_history=(),
                local_false_count=0,
                structural_stop_fraction=None,
                attained_stop_phase=StopPhase.NONE,
                entry_pending=False,
            ),
            reason="local_confirmation_rearmed",
        )

    directions = [direction for direction in (1, -1) if confirmed[direction]]
    if not directions:
        return _result(current, reason="awaiting_local_confirmation")
    if len(directions) != 1:
        raise ValueError("local long and short confirmations cannot coexist")
    direction = directions[0]
    distance_ema55 = direction * numeric[config.columns.distance_15m_ema55]
    return _entry_transition(
        replace(current, local_false_count=0),
        direction=direction,
        distance=distance_ema55,
        config=config,
        reason="debounced_local_ema_confirmation",
    )


def _append_local_history(
    history: tuple[bool, ...], value: bool, config: EmaLifecycleConfig
) -> tuple[bool, ...]:
    return (*history, bool(value))[-config.local_debounce_window_15m :]


def _transition_expansion(
    previous: EmaLifecycleState,
    current: EmaLifecycleState,
    *,
    observation: Mapping[str, Any],
    numeric: dict[str, float],
    config: EmaLifecycleConfig,
    new_15m: bool,
) -> EmaLifecycleTransition:
    pullback_mode = config.entry_mode == "expansion_pullback_recross"
    try:
        distance_ema21 = float(observation["15m_close_to_ema21_log"])
        events = {
            1: bool(observation["transition_long_event"]),
            -1: bool(observation["transition_short_event"]),
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "transition expansion requires event flags and finite 15m EMA distance"
        ) from exc
    if not math.isfinite(distance_ema21):
        raise ValueError(
            "transition expansion requires event flags and finite 15m EMA distance"
        )
    if events[1] and events[-1]:
        raise ValueError("long and short transition events cannot coexist")

    raw_confirmation = {
        direction: (
            direction * distance_ema21 > 0.0
            and direction * numeric[config.columns.slope_15m] > 0.0
            and direction * numeric[config.columns.slope_5m] > 0.0
        )
        for direction in (1, -1)
    }
    if previous.phase in (LifecyclePhase.LONG_ENTERED, LifecyclePhase.SHORT_ENTERED):
        if previous.entry_pending:
            raise ValueError("pending EMA entry requires execution confirmation")
        direction = previous.direction
        false_count = previous.local_false_count
        if new_15m:
            false_count = 0 if raw_confirmation[direction] else false_count + 1
        stop_phase = _later_stop_phase(
            previous.attained_stop_phase,
            _stop_phase(numeric[config.columns.position_r]),
        )
        current = replace(
            current,
            local_false_count=false_count,
            attained_stop_phase=stop_phase,
        )
        if events[-direction] or false_count >= config.local_reset_required_15m:
            reason = (
                (
                    "opposite_expansion_pullback"
                    if pullback_mode else "opposite_transition_expansion"
                )
                if events[-direction]
                else (
                    "expansion_pullback_local_confirmation_lost"
                    if pullback_mode else "transition_local_confirmation_lost"
                )
            )
            return _result(
                replace(current, phase=_CONSUMED[direction]),
                intent=LifecycleIntent.EXIT,
                reason=reason,
                stop_fraction=previous.structural_stop_fraction,
                stop_eligible=True,
                stop_phase=stop_phase,
            )
        return _result(
            current,
            intent=LifecycleIntent.HOLD,
            reason=(
                "expansion_pullback_position_hold"
                if pullback_mode else "transition_position_hold"
            ),
            stop_fraction=previous.structural_stop_fraction,
            stop_eligible=True,
            stop_phase=stop_phase,
        )

    if previous.phase is not LifecyclePhase.UNARMED:
        return _result(
            replace(
                current,
                phase=LifecyclePhase.UNARMED,
                local_false_count=0,
                structural_stop_fraction=None,
                attained_stop_phase=StopPhase.NONE,
                entry_pending=False,
            ),
            reason=(
                "expansion_pullback_event_consumed"
                if pullback_mode else "transition_event_consumed"
            ),
        )
    directions = [direction for direction in (1, -1) if events[direction]]
    if not directions:
        return _result(
            current,
            reason=(
                "awaiting_expansion_pullback_recross"
                if pullback_mode else "awaiting_transition_expansion"
            ),
        )
    direction = directions[0]
    distance_ema55 = direction * numeric[config.columns.distance_15m_ema55]
    return _entry_transition(
        replace(current, local_false_count=0),
        direction=direction,
        distance=distance_ema55,
        config=config,
        reason=(
            "causal_expansion_pullback_recross"
            if pullback_mode else "causal_transition_expansion"
        ),
    )


def _advance_rearm(
    state: EmaLifecycleState,
    numeric: dict[str, float],
    config: EmaLifecycleConfig,
    consumed_direction: int,
    new_4h: bool,
) -> EmaLifecycleTransition:
    count = state.rearm_break_count
    if (
        new_4h
        and consumed_direction * numeric[config.columns.alignment_4h]
        < config.full_alignment
    ):
        count += 1
    phase = _REARMING[consumed_direction] if count else _CONSUMED[consumed_direction]
    state = replace(state, phase=phase, rearm_break_count=count)
    if count < config.rearm_4h_breaks:
        return _result(state, reason="entry_epoch_consumed")
    direction = _slow_epoch_direction(numeric, config)
    if direction:
        return _result(
            replace(
                state,
                phase=_SETUP[direction],
                pullback_15m_at=None,
                one_hour_break_count=0,
                rearm_break_count=0,
                structural_stop_fraction=None,
                attained_stop_phase=StopPhase.NONE,
                entry_pending=False,
            ),
            reason="new_persistent_epoch",
        )
    return _result(state, reason="awaiting_new_persistent_epoch")


def _slow_epoch_direction(numeric: dict[str, float], config: EmaLifecycleConfig) -> int:
    for direction in (1, -1):
        if _slow_geometry(numeric, config, direction) and (
            direction * numeric[config.columns.persistence_4h]
            >= config.minimum_persistence_4h_bars
        ):
            return direction
    return 0


def _entry_transition(
    state: EmaLifecycleState,
    *,
    direction: int,
    distance: float,
    config: EmaLifecycleConfig,
    reason: str,
) -> EmaLifecycleTransition:
    stop_fraction = _structural_stop_fraction(distance, direction)
    eligible = config.minimum_stop_fraction <= stop_fraction <= config.maximum_stop_fraction
    if not eligible:
        return _result(
            state,
            reason="structural_stop_outside_bounds",
            stop_fraction=stop_fraction,
            stop_eligible=False,
        )
    entered = replace(
        state,
        phase=_ENTERED[direction],
        one_hour_break_count=0,
        structural_stop_fraction=stop_fraction,
        attained_stop_phase=StopPhase.STRUCTURAL_INITIAL,
        entry_pending=True,
    )
    return _result(
        entered,
        intent=LifecycleIntent.ACCEPT_ENTRY,
        reason=reason,
        stop_fraction=stop_fraction,
        stop_eligible=True,
        stop_phase=StopPhase.STRUCTURAL_INITIAL,
    )


def _slow_geometry(
    numeric: dict[str, float], config: EmaLifecycleConfig, direction: int
) -> bool:
    columns = config.columns
    return (
        direction * numeric[columns.alignment_1d] >= config.full_alignment
        and direction * numeric[columns.alignment_4h] >= config.full_alignment
        and direction * numeric[columns.slope_1d] > 0.0
        and direction * numeric[columns.slope_4h] > 0.0
    )


def _stop_phase(r_multiple: float) -> StopPhase:
    if r_multiple >= 2.0:
        return StopPhase.EMA1H_21
    if r_multiple >= 1.0:
        return StopPhase.EMA15_55
    return StopPhase.STRUCTURAL_INITIAL


def _later_stop_phase(left: StopPhase, right: StopPhase) -> StopPhase:
    order = {
        StopPhase.NONE: 0,
        StopPhase.STRUCTURAL_INITIAL: 1,
        StopPhase.EMA15_55: 2,
        StopPhase.EMA1H_21: 3,
    }
    return left if order[left] >= order[right] else right


def _structural_stop_fraction(distance: float, direction: int) -> float:
    if direction == 1:
        return -math.expm1(-distance)
    if direction == -1:
        return math.expm1(distance)
    raise ValueError("structural stop direction must be signed")


def _validated_observation(
    state: EmaLifecycleState,
    observation: Mapping[str, Any],
    config: EmaLifecycleConfig,
) -> dict[str, Any]:
    if not isinstance(state, EmaLifecycleState):
        raise TypeError("state must be EmaLifecycleState")
    columns = config.columns
    timestamp_names = (
        columns.decision_at,
        columns.completed_15m_at,
        columns.completed_1h_at,
        columns.completed_4h_at,
    )
    numeric_names = (
        columns.alignment_1d,
        columns.alignment_4h,
        columns.persistence_4h,
        columns.slope_1d,
        columns.slope_4h,
        columns.distance_15m_ema55,
        columns.slope_15m,
        columns.slope_5m,
        columns.distance_1h_ema21,
        columns.position_r,
    )
    missing = set((*timestamp_names, *numeric_names)) - set(observation)
    if missing:
        raise ValueError(f"EMA lifecycle missing columns: {sorted(missing)}")
    try:
        timestamps = tuple(_utc_timestamp(observation[name]) for name in timestamp_names)
        numeric = {name: float(observation[name]) for name in numeric_names}
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("EMA lifecycle observation contains invalid values") from exc
    if not all(math.isfinite(value) for value in numeric.values()):
        raise ValueError("EMA lifecycle observation contains non-finite values")
    decision_at = timestamps[0]
    if any(completed_at > decision_at for completed_at in timestamps[1:]):
        raise ValueError("EMA lifecycle received an incomplete higher-timeframe candle")
    if state.last_decision_at is not None and decision_at <= state.last_decision_at:
        raise ValueError("EMA lifecycle decisions must be strictly increasing")
    if state.last_4h_at is not None and timestamps[3] == state.last_4h_at:
        repeated_alignment = numeric[columns.alignment_4h]
        repeated_persistence = numeric[columns.persistence_4h]
        if (
            repeated_alignment != state.last_4h_alignment
            or repeated_persistence != state.last_4h_persistence
        ):
            raise ValueError("repeated completed 4h candle changed EMA persistence")
    elif state.last_4h_at is not None:
        if timestamps[3] - state.last_4h_at != pd.Timedelta(hours=4):
            raise ValueError("completed 4h candles must advance by exactly four hours")
        alignment = numeric[columns.alignment_4h]
        previous_alignment = state.last_4h_alignment
        previous_persistence = state.last_4h_persistence
        expected_persistence = (
            previous_persistence + alignment
            if alignment != 0.0 and alignment == previous_alignment
            else alignment
        )
        if numeric[columns.persistence_4h] != expected_persistence:
            raise ValueError("completed 4h EMA persistence did not advance causally")
    for label, completed_at, previous in zip(
        ("15m", "1h", "4h"), timestamps[1:],
        (state.last_15m_at, state.last_1h_at, state.last_4h_at),
    ):
        if previous is not None and completed_at < previous:
            raise ValueError(f"EMA lifecycle {label} completion moved backward")
    for label, completed_at, cadence in zip(
        ("15m", "1h", "4h"),
        timestamps[1:],
        (pd.Timedelta(minutes=15), pd.Timedelta(hours=1), pd.Timedelta(hours=4)),
    ):
        if completed_at.value % cadence.value:
            raise ValueError(
                f"EMA lifecycle {label} completion is not UTC boundary aligned"
            )
    if state.phase not in (LifecyclePhase.LONG_ENTERED, LifecyclePhase.SHORT_ENTERED):
        # R is execution-owned and ignored while flat, but it must remain explicit.
        numeric[columns.position_r] = 0.0
    return {"timestamps": timestamps, "numeric": numeric}


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("timestamp is missing")
    if timestamp.tzinfo is None:
        raise ValueError("EMA lifecycle timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _result(
    state: EmaLifecycleState,
    *,
    intent: LifecycleIntent = LifecycleIntent.NONE,
    reason: str,
    stop_fraction: float | None = None,
    stop_eligible: bool = False,
    stop_phase: StopPhase = StopPhase.NONE,
) -> EmaLifecycleTransition:
    direction = state.direction
    return EmaLifecycleTransition(
        state=state,
        intent=intent,
        reason=reason,
        direction=direction,
        accept_entry=intent is LifecycleIntent.ACCEPT_ENTRY,
        hold=intent is LifecycleIntent.HOLD,
        exit=intent is LifecycleIntent.EXIT,
        structural_stop_fraction=stop_fraction,
        structural_stop_eligible=stop_eligible,
        stop_phase=stop_phase,
        trailing_enabled=stop_phase in (StopPhase.EMA15_55, StopPhase.EMA1H_21),
    )
