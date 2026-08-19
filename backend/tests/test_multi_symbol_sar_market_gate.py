from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.evaluation.sweep_multi_symbol_sar_market_gate import (
    _close_position,
    _gate_allows,
    completed_btc_regime,
)


def _btc_bars(periods: int = 144) -> pd.DataFrame:
    close = np.linspace(100.0, 120.0, periods)
    return pd.DataFrame(
        {
            "open_time": np.arange(periods, dtype=np.int64) * 300_000,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        }
    )


def test_btc_regime_ignores_uncompleted_four_hour_bar() -> None:
    bars = _btc_bars()
    baseline = completed_btc_regime(bars)
    changed = bars.copy()
    changed.loc[48:94, "close"] *= 10.0
    actual = completed_btc_regime(changed)

    pd.testing.assert_frame_equal(baseline.iloc[:95], actual.iloc[:95])


@pytest.mark.parametrize(
    ("direction", "gate", "trend", "momentum", "expected"),
    [
        (1, 0, -1, -1, True),
        (1, 1, 1, -1, True),
        (1, 1, -1, 1, False),
        (-1, 2, 1, 1, True),
        (1, 2, -1, 1, False),
        (-1, 3, 1, 1, True),
        (1, 4, 1, -1, False),
        (-1, 5, -1, -1, True),
    ],
)
def test_market_gate_contract(direction, gate, trend, momentum, expected) -> None:
    assert bool(_gate_allows(direction, gate, trend, momentum)) is expected


@pytest.mark.parametrize(
    ("direction", "reference", "entry_notional", "exit_fee", "gross"),
    [(1, 110.0, 100.0, 0.11, 10.0), (-1, 90.0, 100.0, 0.09, 10.0)],
)
def test_close_position_uses_exact_entry_notional(
    direction: int,
    reference: float,
    entry_notional: float,
    exit_fee: float,
    gross: float,
) -> None:
    slippage_adjusted_reference = reference / (1.0 - direction * 0.0002)
    cash, net, fee = _close_position(
        999.9,
        direction,
        1,
        1.0,
        slippage_adjusted_reference,
        entry_notional,
        0.1,
        0.0,
    )

    assert fee == pytest.approx(exit_fee)
    assert net == pytest.approx(gross - 0.1 - exit_fee)
    assert cash == pytest.approx(999.9 + gross - exit_fee)
