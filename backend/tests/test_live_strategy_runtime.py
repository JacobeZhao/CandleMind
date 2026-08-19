from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backend.app.services.live_strategy_runtime import (
    LiveStrategyRuntime,
    LiveStrategyRuntimeError,
)
from backend.app.strategies.sar_pyramid import PositionSnapshot, SarPyramidActionType


def _bars(count: int = 500) -> pd.DataFrame:
    opened = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    close = 100.0 + np.arange(count) * 0.03 + np.sin(np.arange(count) / 10.0)
    return pd.DataFrame(
        {
            "open_time": opened,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "close_time": opened + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
        }
    )


def _baseline(runtime: LiveStrategyRuntime, bars: pd.DataFrame) -> None:
    cutoff = bars.iloc[-2]["open_time"] + pd.Timedelta(seconds=15)
    plan = runtime.prepare_decision(bars.iloc[:-1], PositionSnapshot(), server_time=cutoff)
    assert plan is not None and plan.no_action_reason == "baseline"
    runtime.commit(plan)


def test_first_observation_prepares_baseline_without_action() -> None:
    bars = _bars()
    saved = []
    runtime = LiveStrategyRuntime("solusdt", save_state=saved.append)
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)

    plan = runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff)

    assert plan is not None
    assert plan.actions == ()
    assert plan.no_action_reason == "baseline"
    assert runtime.last_processed_decision_time is None
    runtime.commit(plan)
    assert runtime.last_processed_decision_time == bars.iloc[-2]["close_time"]
    assert saved[-1]["symbol"] == "SOLUSDT"
    assert "broker" not in saved[-1]


def test_no_action_plan_has_reason_and_requires_commit() -> None:
    bars = _bars()
    runtime = LiveStrategyRuntime("SOLUSDT")
    _baseline(runtime, bars)
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)

    plan = runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff)

    assert plan is not None
    assert plan.actions == ()
    assert plan.no_action_reason == "no_strategy_action"
    before = runtime.state
    with pytest.raises(LiveStrategyRuntimeError, match="not fully confirmed"):
        runtime.commit(plan, actions_confirmed=False)
    assert runtime.state == before
    assert runtime.pending_plan == plan


def test_open_plan_uses_proposed_state_without_committing(monkeypatch) -> None:
    bars = _bars()
    runtime = LiveStrategyRuntime("SOLUSDT")
    _baseline(runtime, bars)
    runtime.state = replace(runtime.state, aligned_run=6, aligned_run_direction=1)
    original = runtime._indicator_tape

    def reversal_tape(frame):
        tape = original(frame)
        index = len(tape) - 2
        tape.at[index, "sar_direction"] = 1
        tape.at[index, "sar_reversal"] = True
        tape.at[index, "trend_direction"] = 1
        tape.at[index, "entry_trend_direction"] = 1
        tape.at[index - 1, "trend_direction"] = 0
        return tape

    monkeypatch.setattr(runtime, "_indicator_tape", reversal_tape)
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)

    plan = runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff)

    assert plan is not None
    assert [action.action for action in plan.actions] == [SarPyramidActionType.OPEN]
    assert plan.actions[0].direction == 1
    assert runtime.state.regime_entry_count == 0
    assert plan.proposed_state.regime_entry_count == 1


def test_commit_persists_before_advancing_memory() -> None:
    bars = _bars()
    runtime = LiveStrategyRuntime("SOLUSDT", save_state=lambda _payload: (_ for _ in ()).throw(OSError("disk full")))
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)
    plan = runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff)

    with pytest.raises(OSError, match="disk full"):
        runtime.commit(plan)

    assert runtime.last_processed_decision_time is None
    assert runtime.pending_plan == plan


def test_duplicate_bar_returns_pending_plan_then_none_after_commit() -> None:
    bars = _bars()
    runtime = LiveStrategyRuntime("SOLUSDT")
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)
    first = runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff)

    assert runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff) is first
    runtime.commit(first)
    assert runtime.prepare_decision(bars, PositionSnapshot(), server_time=cutoff) is None


def test_gap_is_rejected() -> None:
    bars = _bars().drop(index=100).reset_index(drop=True)
    runtime = LiveStrategyRuntime("SOLUSDT")

    with pytest.raises(LiveStrategyRuntimeError, match="gap"):
        runtime.prepare_decision(
            bars,
            PositionSnapshot(),
            server_time=bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15),
        )


def test_restore_uses_live_payload_without_paper_state() -> None:
    bars = _bars()
    decision_time = bars.iloc[-2]["close_time"]
    runtime = LiveStrategyRuntime(
        "SOLUSDT",
        restored_payload={
            "symbol": "SOLUSDT",
            "strategy": {"armed": True},
            "last_processed_decision_time": decision_time.isoformat(),
            "last_execution_open_time": None,
        },
    )

    assert runtime.state.armed is True
    assert runtime.last_processed_decision_time == decision_time
