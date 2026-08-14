from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.strategies import sar_pyramid
from backend.app.strategies.sar_pyramid import (
    SarPyramidConfig,
    adx_regime,
    hourly_adx_regime,
    parabolic_sar,
    run_sar_pyramid_backtest,
)


def _bars(closes: list[float]) -> pd.DataFrame:
    opened = pd.date_range("2025-01-01", periods=len(closes), freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": opened,
            "open": [100.0] * len(closes),
            "high": [max(101.0, value) for value in closes],
            "low": [min(99.0, value) for value in closes],
            "close": closes,
        }
    )


def _mock_sar(monkeypatch: pytest.MonkeyPatch, directions: list[int], flips: list[int]) -> None:
    def calculate(frame: pd.DataFrame, **_: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "psar": [np.nan, *([95.0] * (len(frame) - 1))],
                "sar_direction": directions,
                "sar_reversal": [index in flips for index in range(len(frame))],
            },
            index=frame.index,
        )

    monkeypatch.setattr(sar_pyramid, "parabolic_sar", calculate)


def _mock_regime(monkeypatch: pytest.MonkeyPatch, directions: list[int]) -> None:
    def calculate(frame: pd.DataFrame, **_: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "adx_1h": [30.0 if value else 10.0 for value in directions],
                "plus_di_1h": [30.0 if value > 0 else 10.0 for value in directions],
                "minus_di_1h": [30.0 if value < 0 else 10.0 for value in directions],
                "adx_rising": [True] * len(frame),
                "di_spread_1h": [20.0 if value else 0.0 for value in directions],
                "adx_available_at": frame["open_time"] + pd.Timedelta(minutes=5),
                "trend_direction": directions,
                "entry_trend_direction": directions,
            },
            index=frame.index,
        )

    monkeypatch.setattr(sar_pyramid, "adx_regime", calculate)


def test_psar_matches_frozen_wilder_vector_and_is_prefix_stable() -> None:
    bars = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0, 12.0, 10.0],
            "low": [9.0, 9.5, 10.0, 11.0, 9.0, 8.0],
            "close": [9.5, 10.5, 11.5, 12.5, 9.5, 8.5],
        }
    )
    result = parabolic_sar(bars)

    assert result["psar"].to_numpy() == pytest.approx(
        [np.nan, 9.0, 9.0, 9.12, 13.0, 13.0], nan_ok=True
    )
    assert result["sar_direction"].tolist() == [0, 1, 1, 1, -1, -1]
    assert result["sar_reversal"].tolist() == [False, False, False, False, True, False]
    pd.testing.assert_frame_equal(result.iloc[:5], parabolic_sar(bars.iloc[:5]))


def test_long_pullback_recapture_adds_once_and_uses_next_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100, 100, 100, 99, 101, 101, 99, 101, 101])
    _mock_sar(monkeypatch, [0, 1, 1, 1, 1, 1, 1, 1, 1], [2])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0),
    )

    entries = result.fills[result.fills["action"].isin(["open", "add"])]
    assert entries["time"].tolist() == [bars.at[3, "open_time"], bars.at[5, "open_time"], bars.at[8, "open_time"]]
    assert entries["layer"].tolist() == [1, 2, 3]


def test_short_rule_is_symmetric_and_five_layers_is_a_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closes = [100, 100, 100, 101, 99, 101, 99, 101, 99, 101, 99, 101, 99, 101, 99]
    bars = _bars(closes)
    _mock_sar(monkeypatch, [0, -1, -1, *([-1] * (len(bars) - 3))], [2])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0),
    )

    entries = result.fills[result.fills["action"].isin(["open", "add"])]
    assert entries["layer"].tolist() == [1, 2, 3, 4, 5]
    assert result.metrics["add_count"] == 4


def test_reversal_has_priority_over_recapture_and_charges_two_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100, 100, 100, 99, 101, 101, 101])
    _mock_sar(monkeypatch, [0, 1, 1, 1, -1, -1, -1], [2, 4])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(fee_rate=0.001, slippage_rate=0.0),
    )

    at_reversal = result.fills[result.fills["time"] == bars.at[5, "open_time"]]
    assert at_reversal["action"].tolist() == ["reverse_close", "open"]
    assert "add" not in at_reversal["action"].tolist()
    assert result.metrics["fees"] > 0.0


def test_observed_funding_is_signed_by_position_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 7)
    _mock_sar(monkeypatch, [0, 1, 1, 1, 1, 1, 1], [2])
    funding = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "available_at": [bars.at[4, "open_time"]],
            "funding_rate": [0.001],
        }
    )
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        funding=funding,
        config=SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0),
    )

    assert result.metrics["funding_pnl"] == pytest.approx(-2.0)
    assert result.metrics["final_equity"] == pytest.approx(9_998.0)


def test_pit_ineligibility_closes_position_and_blocks_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 8)
    _mock_sar(monkeypatch, [0, 1, 1, 1, -1, -1, 1, 1], [2, 4, 6])
    eligibility = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "effective_from": [bars.at[0, "open_time"]],
            "effective_to": [bars.at[5, "open_time"]],
            "eligible": [True],
        }
    )
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        eligibility=eligibility,
        config=SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0),
    )

    assert result.cycles["exit_reason"].tolist() == ["universe_exit"]
    assert result.fills["action"].tolist() == ["open", "universe_exit"]


def test_hourly_adx_uses_only_completed_hours_and_is_prefix_stable() -> None:
    count = 60 * 12
    opened = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    close = 100.0 + np.arange(count) * 0.01 + np.sin(np.arange(count) / 9.0)
    bars = pd.DataFrame(
        {
            "open_time": opened,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        }
    )
    prefix_rows = 50 * 12
    full = hourly_adx_regime(bars)
    prefix = hourly_adx_regime(bars.iloc[:prefix_rows])

    pd.testing.assert_frame_equal(full.iloc[:prefix_rows], prefix)
    known = full["adx_available_at"].notna()
    decisions = bars["open_time"] + pd.Timedelta(minutes=5)
    assert (full.loc[known, "adx_available_at"] <= decisions.loc[known]).all()


@pytest.mark.parametrize("timeframe", ["15min", "30min", "1h", "2h", "4h"])
def test_adx_filter_timeframes_are_causal(timeframe: str) -> None:
    count = 160 * 12
    opened = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    close = 100.0 + np.arange(count) * 0.01 + np.sin(np.arange(count) / 11.0)
    bars = pd.DataFrame(
        {
            "open_time": opened,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        }
    )
    result = adx_regime(bars, timeframe=timeframe)
    known = result["adx_available_at"].notna()
    decisions = bars["open_time"] + pd.Timedelta(minutes=5)
    assert (result.loc[known, "adx_available_at"] <= decisions.loc[known]).all()


def test_adx_direction_blocks_countertrend_sar_reversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 8)
    _mock_sar(monkeypatch, [0, 1, 1, 1, -1, -1, -1, -1], [2, 4])
    _mock_regime(monkeypatch, [1, 1, 1, 1, 1, 1, 1, 1])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(
            fee_rate=0.0, slippage_rate=0.0, use_adx_filter=True
        ),
    )

    assert result.fills["action"].tolist() == ["open", "reverse_close"]
    assert result.cycles["direction"].tolist() == [1]


def test_adx_confirmation_can_open_an_already_aligned_sar_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 7)
    _mock_sar(monkeypatch, [0, 1, 1, 1, 1, 1, 1], [])
    _mock_regime(monkeypatch, [0, 0, 0, 1, 1, 1, 1])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="BTCUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(
            fee_rate=0.0, slippage_rate=0.0, use_adx_filter=True
        ),
    )

    assert result.fills.iloc[0]["action"] == "open"
    assert result.fills.iloc[0]["time"] == bars.at[4, "open_time"]


def test_sar_entry_confirmation_rejects_short_aligned_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 9)
    _mock_sar(monkeypatch, [0, -1, 1, 1, 1, 1, 1, 1, 1], [2])
    _mock_regime(monkeypatch, [1] * len(bars))
    result = run_sar_pyramid_backtest(
        bars,
        symbol="SOLUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            use_adx_filter=True,
            entry_confirmation_bars=2,
        ),
    )

    assert result.fills.iloc[0]["action"] == "open"
    assert result.fills.iloc[0]["time"] == bars.at[5, "open_time"]


def test_recapture_buffer_and_progressive_fill_gate_adds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100, 100, 100, 99, 100.3, 100.3, 100.3, 100.3])
    bars.loc[5, "open"] = 99.5
    bars.loc[6, "open"] = 100.4
    _mock_sar(monkeypatch, [0, 1, 1, 1, 1, 1, 1, 1], [2])
    result = run_sar_pyramid_backtest(
        bars,
        symbol="SOLUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            recapture_buffer_fraction=0.0024,
            require_progressive_adds=True,
        ),
    )

    entries = result.fills[result.fills["action"].isin(["open", "add"])]
    assert entries["time"].tolist() == [bars.at[3, "open_time"], bars.at[6, "open_time"]]
    assert result.metrics["rejected_add_count"] == 1


def test_adx_regime_entry_cap_blocks_repeated_sar_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars([100] * 11)
    directions = [0, -1, 1, 1, -1, -1, 1, 1, -1, -1, 1]
    _mock_sar(monkeypatch, directions, [2, 4, 6, 8, 10])
    _mock_regime(monkeypatch, [1] * len(bars))
    result = run_sar_pyramid_backtest(
        bars,
        symbol="SOLUSDT",
        start="2025-01-01",
        end="2025-01-02",
        config=SarPyramidConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            use_adx_filter=True,
            max_entries_per_adx_regime=1,
        ),
    )

    assert result.fills["action"].tolist() == ["open", "reverse_close"]
