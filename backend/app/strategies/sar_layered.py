"""Pure completed-bar state machine for layered Parabolic-SAR strategies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import math


BAR_INTERVAL = timedelta(minutes=5)
BAR_CLOSE_OFFSET = timedelta(milliseconds=1)


class SarLayerMode(str, Enum):
    MARTINGALE = "martingale"
    ANTI_MARTINGALE = "anti_martingale"


@dataclass(frozen=True, slots=True)
class SarLayeredConfig:
    mode: SarLayerMode
    sar_step: float = 0.02
    sar_max: float = 0.20
    max_layers: int = 4
    multiplier: float = 1.5
    trigger_fraction: float = 0.005

    def validate(self) -> None:
        if not math.isfinite(self.sar_step) or not 0.001 <= self.sar_step <= 0.1:
            raise ValueError("sar_step must be in [0.001, 0.1]")
        if not math.isfinite(self.sar_max) or not self.sar_step <= self.sar_max <= 0.5:
            raise ValueError("sar_max must be in [sar_step, 0.5]")
        if (
            not isinstance(self.max_layers, int)
            or isinstance(self.max_layers, bool)
            or not 1 <= self.max_layers <= 6
        ):
            raise ValueError("max_layers must be an integer in [1, 6]")
        if not math.isfinite(self.multiplier) or not 1.0 <= self.multiplier <= 1.8:
            raise ValueError("multiplier must be in [1.0, 1.8]")
        if (
            not math.isfinite(self.trigger_fraction)
            or not 0.001 <= self.trigger_fraction <= 0.05
        ):
            raise ValueError("trigger_fraction must be in [0.001, 0.05]")
        if not isinstance(self.mode, SarLayerMode):
            raise ValueError("mode must be a SarLayerMode")

    @property
    def layer_weights(self) -> tuple[float, ...]:
        self.validate()
        return geometric_layer_weights(self.max_layers, self.multiplier)

    @property
    def layers(self) -> int:
        return self.max_layers

    @property
    def target_notional_fraction(self) -> float:
        return 1.0


@dataclass(frozen=True, slots=True)
class SarLayeredState:
    """Persist this value after each accepted completed bar."""

    last_processed_close_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class SarLayeredSignal:
    bar_open_time: datetime
    bar_close_time: datetime
    observed_at: datetime
    decision_close: float
    sar_direction: int
    sar_reversal: bool


@dataclass(frozen=True, slots=True)
class LayeredPositionSnapshot:
    direction: int = 0
    layers: int = 0
    last_fill_price: float | None = None


class SarLayeredActionType(str, Enum):
    OPEN = "open"
    ADD = "add"
    REVERSE_CLOSE = "reverse_close"


@dataclass(frozen=True, slots=True)
class SarLayeredAction:
    action: SarLayeredActionType
    direction: int
    layer: int | None = None
    capital_weight: float = 0.0


@dataclass(frozen=True, slots=True)
class SarLayeredTransition:
    state: SarLayeredState
    actions: tuple[SarLayeredAction, ...]


def geometric_layer_weights(max_layers: int, multiplier: float) -> tuple[float, ...]:
    """Return geometric layer weights normalized to exactly one budget."""

    if (
        not isinstance(max_layers, int)
        or isinstance(max_layers, bool)
        or not 1 <= max_layers <= 6
    ):
        raise ValueError("max_layers must be an integer in [1, 6]")
    if not math.isfinite(multiplier) or not 1.0 <= multiplier <= 1.8:
        raise ValueError("multiplier must be in [1.0, 1.8]")
    raw = tuple(multiplier**index for index in range(max_layers))
    total = math.fsum(raw)
    prefix = tuple(value / total for value in raw[:-1])
    return prefix + (1.0 - math.fsum(prefix),)


def transition_sar_layered(
    state: SarLayeredState,
    signal: SarLayeredSignal,
    position: LayeredPositionSnapshot,
    config: SarLayeredConfig,
) -> SarLayeredTransition:
    """Evaluate one closed 5m candle without mutating execution state.

    The caller owns fills. After a successful OPEN or ADD, it must provide the
    actual fill price as ``last_fill_price`` in the next position snapshot.
    """

    config.validate()
    close_time = _validate_signal(signal)
    _validate_position(position, config)
    previous = _utc(state.last_processed_close_time) if state.last_processed_close_time else None
    if previous is not None and close_time < previous:
        raise ValueError("bars must not be processed out of order")
    if previous == close_time:
        return SarLayeredTransition(state, ())

    next_state = replace(state, last_processed_close_time=close_time)
    weights = config.layer_weights

    if position.direction == 0:
        return SarLayeredTransition(
            next_state,
            (_entry_action(SarLayeredActionType.OPEN, signal.sar_direction, 1, weights),),
        )

    direction_changed = signal.sar_direction != position.direction
    if signal.sar_reversal or direction_changed:
        return SarLayeredTransition(
            next_state,
            (
                SarLayeredAction(SarLayeredActionType.REVERSE_CLOSE, position.direction),
                _entry_action(SarLayeredActionType.OPEN, signal.sar_direction, 1, weights),
            ),
        )

    if position.layers >= config.max_layers:
        return SarLayeredTransition(next_state, ())
    assert position.last_fill_price is not None
    if not _layer_triggered(signal.decision_close, position, config):
        return SarLayeredTransition(next_state, ())
    next_layer = position.layers + 1
    return SarLayeredTransition(
        next_state,
        (_entry_action(SarLayeredActionType.ADD, position.direction, next_layer, weights),),
    )


def _entry_action(
    action: SarLayeredActionType,
    direction: int,
    layer: int,
    weights: tuple[float, ...],
) -> SarLayeredAction:
    return SarLayeredAction(action, direction, layer, weights[layer - 1])


def _layer_triggered(
    close: float,
    position: LayeredPositionSnapshot,
    config: SarLayeredConfig,
) -> bool:
    assert position.last_fill_price is not None
    anchor = position.last_fill_price
    favorable_threshold = anchor * (1.0 + position.direction * config.trigger_fraction)
    adverse_threshold = anchor * (1.0 - position.direction * config.trigger_fraction)
    if config.mode is SarLayerMode.MARTINGALE:
        return close <= adverse_threshold if position.direction > 0 else close >= adverse_threshold
    return close >= favorable_threshold if position.direction > 0 else close <= favorable_threshold


def _validate_signal(signal: SarLayeredSignal) -> datetime:
    open_time = _utc(signal.bar_open_time)
    close_time = _utc(signal.bar_close_time)
    observed_at = _utc(signal.observed_at)
    expected_close = open_time + BAR_INTERVAL - BAR_CLOSE_OFFSET
    if close_time != expected_close:
        raise ValueError("signal must represent exactly one 5m candle")
    if observed_at <= close_time:
        raise ValueError("signal candle is not closed")
    if signal.sar_direction not in (-1, 1):
        raise ValueError("sar_direction must be -1 or 1")
    if not isinstance(signal.sar_reversal, bool):
        raise ValueError("sar_reversal must be a bool")
    if not math.isfinite(signal.decision_close) or signal.decision_close <= 0.0:
        raise ValueError("decision_close must be finite and positive")
    return close_time


def _validate_position(
    position: LayeredPositionSnapshot, config: SarLayeredConfig
) -> None:
    if position.direction not in (-1, 0, 1):
        raise ValueError("position direction must be -1, 0, or 1")
    if (
        not isinstance(position.layers, int)
        or isinstance(position.layers, bool)
        or not 0 <= position.layers <= config.max_layers
    ):
        raise ValueError("position layers are invalid")
    if position.direction == 0:
        if position.layers != 0 or position.last_fill_price is not None:
            raise ValueError("flat positions cannot have layers or a fill anchor")
        return
    if position.layers < 1:
        raise ValueError("open positions must have at least one layer")
    if (
        position.last_fill_price is None
        or not math.isfinite(position.last_fill_price)
        or position.last_fill_price <= 0.0
    ):
        raise ValueError("open positions require a positive last fill price")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("bar timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
