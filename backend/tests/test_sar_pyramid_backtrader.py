from __future__ import annotations

import pandas as pd
import pytest

from backend.app.strategies.sar_pyramid import SarPyramidConfig
from backend.app.strategies.sar_pyramid_backtrader import (
    _funding_by_open,
    run_backtrader_sar_pyramid,
)


def _signal_frame() -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=10, freq="5min")
    close = [100.0, 100.0, 100.0, 99.0, 101.0, 101.0, 100.0, 100.0, 100.0, 100.0]
    direction = [-1, 1, 1, 1, 1, 1, -1, -1, -1, -1]
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [102.0] * len(index),
            "low": [98.0] * len(index),
            "close": close,
            "volume": [0.0] * len(index),
            "sar_direction": direction,
            "sar_reversal": [False, True, False, False, False, False, True, False, False, False],
            "trend_direction": [1] * len(index),
            "entry_trend_direction": [1] * len(index),
            "eligible": [True] * 9 + [False],
            "funding_rate": [0.0] * len(index),
            "terminal": [False] * 9 + [True],
        },
        index=index,
    )


def test_backtrader_executes_frozen_signals_at_next_open() -> None:
    config = SarPyramidConfig(
        fee_rate=0.001,
        slippage_rate=0.0002,
        target_notional_fraction=0.5,
    )
    result = run_backtrader_sar_pyramid(_signal_frame(), config=config)

    assert result.metrics["engine"] == "backtrader"
    assert result.fills["action"].tolist() == ["open", "add", "reverse_close"]
    assert result.fills["time"].tolist() == [
        pd.Timestamp("2025-01-01T00:10:00Z"),
        pd.Timestamp("2025-01-01T00:25:00Z"),
        pd.Timestamp("2025-01-01T00:35:00Z"),
    ]
    assert result.fills.iloc[0]["price"] == pytest.approx(100.02)
    assert result.fills.iloc[-1]["price"] == pytest.approx(99.98)
    assert result.metrics["commission"] > 0.0
    assert result.metrics["final_equity"] < config.initial_cash
    assert result.trades.iloc[0]["direction"] == 1
    assert result.trades.iloc[0]["max_layers"] == 2
    assert result.trades.iloc[0]["exit_reason"] == "reverse_close"


def test_backtrader_applies_signed_funding_before_same_open_orders() -> None:
    frame = _signal_frame()
    frame.loc[pd.Timestamp("2025-01-01T00:20:00"), "funding_rate"] = 0.001
    result = run_backtrader_sar_pyramid(
        frame,
        config=SarPyramidConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            target_notional_fraction=0.5,
        ),
    )

    assert result.metrics["funding_pnl"] == pytest.approx(-1.0)
    assert result.metrics["final_equity"] == pytest.approx(9_999.0)


def test_funding_availability_is_rounded_forward_without_lookahead() -> None:
    times = pd.Series(pd.date_range("2025-01-01", periods=3, freq="5min", tz="UTC"))
    funding = pd.DataFrame(
        {
            "available_at": [pd.Timestamp("2025-01-01T00:00:00.005Z")],
            "funding_rate": [0.001],
        }
    )

    assert _funding_by_open(times, funding).tolist() == [0.0, 0.001, 0.0]
