from datetime import datetime, timezone

import pandas as pd

from backend.app.services.live_layered_runtime import LiveLayeredStrategyRuntime
from backend.app.strategies.sar_layered import SarLayeredActionType
from backend.app.strategies.sar_pyramid import PositionSnapshot


def _bars(count: int = 6) -> pd.DataFrame:
    opens = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    prices = [100.0 + index for index in range(count)]
    return pd.DataFrame(
        {
            "open_time": opens,
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": [price + 0.5 for price in prices],
            "close_time": opens + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
        }
    )


def _runtime(strategy_type="sar_anti_martingale", restored_payload=None):
    return LiveLayeredStrategyRuntime(
        "SOLUSDT",
        strategy_type=strategy_type,
        config_version=f"{strategy_type}_v1",
        parameters={
            "sar_step": 0.02,
            "sar_max": 0.2,
            "max_layers": 4,
            "layer_multiplier": 1.5,
            "add_trigger_fraction": 0.005,
        },
        restored_payload=restored_payload,
        now=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def test_runtime_baselines_then_opens_from_the_next_completed_bar():
    bars = _bars()
    runtime = _runtime()
    baseline = runtime.prepare_decision(
        bars.iloc[:5],
        PositionSnapshot(),
        server_time=pd.Timestamp("2025-01-01T00:20:10Z"),
        execution_price=104.0,
    )

    assert baseline is not None
    assert baseline.actions == ()
    assert baseline.no_action_reason == "baseline"
    runtime.commit(baseline)

    plan = runtime.prepare_decision(
        bars,
        PositionSnapshot(),
        server_time=pd.Timestamp("2025-01-01T00:25:10Z"),
        execution_price=105.0,
    )

    assert plan is not None
    assert [action.action for action in plan.actions] == [SarLayeredActionType.OPEN]
    assert plan.actions[0].capital_weight == runtime.config.layer_weights[0]


def test_runtime_serialized_state_restores_idempotent_decision_progress():
    runtime = _runtime()
    bars = _bars(5)
    plan = runtime.prepare_decision(
        bars,
        PositionSnapshot(),
        server_time=pd.Timestamp("2025-01-01T00:20:10Z"),
    )
    runtime.commit(plan)
    restored = _runtime(
        restored_payload={
            "symbol": "SOLUSDT",
            "strategy": runtime.serialize_state(runtime.state),
            "last_processed_decision_time": runtime.last_processed_decision_time.isoformat(),
            "last_execution_open_time": None,
        }
    )

    assert restored.prepare_decision(
        bars,
        PositionSnapshot(),
        server_time=pd.Timestamp("2025-01-01T00:20:10Z"),
    ) is None


def test_runtime_closes_an_open_position_when_symbol_is_not_tradable():
    runtime = _runtime()
    bars = _bars()
    baseline = runtime.prepare_decision(
        bars.iloc[:5], PositionSnapshot(), server_time=pd.Timestamp("2025-01-01T00:20:10Z")
    )
    runtime.commit(baseline)
    plan = runtime.prepare_decision(
        bars,
        PositionSnapshot(direction=1, layers=1, anchor=100.0),
        server_time=pd.Timestamp("2025-01-01T00:25:10Z"),
        eligible=False,
    )

    assert plan is not None
    assert [action.action for action in plan.actions] == [SarLayeredActionType.REVERSE_CLOSE]
