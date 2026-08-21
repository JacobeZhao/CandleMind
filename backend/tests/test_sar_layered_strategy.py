from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.strategies.sar_layered import (
    LayeredPositionSnapshot,
    SarLayerMode,
    SarLayeredActionType,
    SarLayeredConfig,
    SarLayeredSignal,
    SarLayeredState,
    geometric_layer_weights,
    transition_sar_layered,
)


OPEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _signal(*, close=100.0, direction=1, reversal=False, offset=0):
    opened = OPEN + timedelta(minutes=5 * offset)
    closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
    return SarLayeredSignal(
        bar_open_time=opened,
        bar_close_time=closed,
        observed_at=closed + timedelta(milliseconds=1),
        decision_close=close,
        sar_direction=direction,
        sar_reversal=reversal,
    )


def test_defaults_and_geometric_weights_are_bounded() -> None:
    config = SarLayeredConfig(mode=SarLayerMode.MARTINGALE)
    assert (config.sar_step, config.sar_max, config.max_layers) == (0.02, 0.20, 4)
    assert (config.multiplier, config.trigger_fraction) == (1.5, 0.005)
    assert sum(config.layer_weights) == pytest.approx(1.0)
    assert config.layer_weights == tuple(sorted(config.layer_weights))
    assert sum(geometric_layer_weights(6, 1.8)) <= 1.0


@pytest.mark.parametrize(
    "changes",
    [
        {"sar_step": 0.0009},
        {"sar_max": 0.501},
        {"sar_step": 0.1, "sar_max": 0.09},
        {"max_layers": 7},
        {"multiplier": 1.81},
        {"trigger_fraction": 0.0009},
    ],
)
def test_configuration_bounds_are_enforced(changes) -> None:
    values = {"mode": SarLayerMode.MARTINGALE, **changes}
    with pytest.raises(ValueError):
        SarLayeredConfig(**values).validate()


@pytest.mark.parametrize(
    ("mode", "direction", "close", "expected"),
    [
        (SarLayerMode.MARTINGALE, 1, 99.5, True),
        (SarLayerMode.MARTINGALE, -1, 100.5, True),
        (SarLayerMode.MARTINGALE, 1, 100.5, False),
        (SarLayerMode.ANTI_MARTINGALE, 1, 100.5, True),
        (SarLayerMode.ANTI_MARTINGALE, -1, 99.5, True),
        (SarLayerMode.ANTI_MARTINGALE, -1, 100.5, False),
    ],
)
def test_layer_trigger_uses_direction_and_last_fill(mode, direction, close, expected) -> None:
    result = transition_sar_layered(
        SarLayeredState(),
        _signal(close=close, direction=direction),
        LayeredPositionSnapshot(direction=direction, layers=1, last_fill_price=100.0),
        SarLayeredConfig(mode=mode),
    )
    assert bool(result.actions) is expected
    if expected:
        assert result.actions[0].action is SarLayeredActionType.ADD
        assert result.actions[0].layer == 2


def test_same_candle_cannot_add_twice_and_max_layers_hold() -> None:
    config = SarLayeredConfig(mode=SarLayerMode.MARTINGALE, max_layers=2)
    first = transition_sar_layered(
        SarLayeredState(),
        _signal(close=99.0),
        LayeredPositionSnapshot(direction=1, layers=1, last_fill_price=100.0),
        config,
    )
    duplicate = transition_sar_layered(
        first.state,
        _signal(close=98.0),
        LayeredPositionSnapshot(direction=1, layers=2, last_fill_price=99.0),
        config,
    )
    full = transition_sar_layered(
        first.state,
        _signal(close=98.0, offset=1),
        LayeredPositionSnapshot(direction=1, layers=2, last_fill_price=99.0),
        config,
    )
    assert not duplicate.actions
    assert not full.actions


def test_reversal_closes_before_opposite_first_layer() -> None:
    config = SarLayeredConfig(mode=SarLayerMode.ANTI_MARTINGALE)
    result = transition_sar_layered(
        SarLayeredState(),
        _signal(direction=-1, reversal=True),
        LayeredPositionSnapshot(direction=1, layers=3, last_fill_price=101.0),
        config,
    )
    assert [action.action for action in result.actions] == [
        SarLayeredActionType.REVERSE_CLOSE,
        SarLayeredActionType.OPEN,
    ]
    assert result.actions[1].direction == -1
    assert result.actions[1].layer == 1


def test_rejects_unclosed_or_non_5m_signal() -> None:
    signal = _signal()
    with pytest.raises(ValueError, match="not closed"):
        transition_sar_layered(
            SarLayeredState(),
            replace(signal, observed_at=signal.bar_close_time),
            LayeredPositionSnapshot(),
            SarLayeredConfig(mode=SarLayerMode.MARTINGALE),
        )
    with pytest.raises(ValueError, match="exactly one 5m"):
        transition_sar_layered(
            SarLayeredState(),
            replace(signal, bar_close_time=signal.bar_close_time + timedelta(minutes=1)),
            LayeredPositionSnapshot(),
            SarLayeredConfig(mode=SarLayerMode.MARTINGALE),
        )


def test_restored_state_rejects_an_older_bar() -> None:
    state = SarLayeredState(last_processed_close_time=_signal(offset=1).bar_close_time)
    with pytest.raises(ValueError, match="out of order"):
        transition_sar_layered(
            state,
            _signal(offset=0),
            LayeredPositionSnapshot(),
            SarLayeredConfig(mode=SarLayerMode.MARTINGALE),
        )
