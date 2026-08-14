from __future__ import annotations

from backend.app.strategies.sar_pyramid import (
    PositionSnapshot,
    SarPyramidActionType,
    SarPyramidConfig,
    SarPyramidSignal,
    SarPyramidState,
    transition_sar_pyramid,
)


def _signal(**changes) -> SarPyramidSignal:
    values = dict(
        sar_direction=1,
        sar_reversal=False,
        trend_direction=1,
        entry_trend_direction=1,
        previous_trend_direction=1,
        decision_close=100.0,
    )
    values.update(changes)
    return SarPyramidSignal(**values)


def test_reversal_closes_before_opening_opposite_direction() -> None:
    result = transition_sar_pyramid(
        SarPyramidState(aligned_run=6, aligned_run_direction=1, regime_direction=-1),
        _signal(sar_direction=1, sar_reversal=True, previous_trend_direction=-1),
        PositionSnapshot(direction=-1, layers=2, anchor=101.0),
        100.0,
        SarPyramidConfig(use_adx_filter=True, entry_confirmation_bars=6),
    )

    assert [item.action for item in result.actions] == [
        SarPyramidActionType.REVERSE_CLOSE,
        SarPyramidActionType.OPEN,
    ]


def test_missing_adx_blocks_entry_but_still_allows_risk_exit() -> None:
    result = transition_sar_pyramid(
        SarPyramidState(),
        _signal(trend_direction=0, entry_trend_direction=0),
        PositionSnapshot(direction=1, layers=1, anchor=100.0),
        99.0,
        SarPyramidConfig(use_adx_filter=True),
    )
    assert [item.action for item in result.actions] == [SarPyramidActionType.TREND_FILTER_EXIT]


def test_pullback_recapture_arms_then_adds_once() -> None:
    config = SarPyramidConfig(recapture_buffer_fraction=0.0024)
    armed = transition_sar_pyramid(
        SarPyramidState(), _signal(decision_close=99.0),
        PositionSnapshot(direction=1, layers=1, anchor=100.0), 99.5, config,
    )
    assert armed.state.armed and not armed.actions
    added = transition_sar_pyramid(
        armed.state, _signal(decision_close=100.3),
        PositionSnapshot(direction=1, layers=1, anchor=100.0), 100.4, config,
    )
    assert [item.action for item in added.actions] == [SarPyramidActionType.ADD]
    assert not added.state.armed
