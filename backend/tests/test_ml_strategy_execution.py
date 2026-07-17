import pandas as pd
import pytest

from backend.app.services.ml_strategy import MLTrendParams, simulate_ml_trend


BAR_MS = 5 * 60_000


def _params(**overrides):
    values = {
        "entry_long_threshold": 0.6,
        "entry_short_threshold": 0.6,
        "min_prob_gap": 0.1,
        "min_prob_gap_large_cap": 0.1,
        "exit_threshold": 0.4,
        "reversal_threshold": 0.8,
        "initial_stop_mult": 10.0,
        "atr_trail": 10.0,
        "max_adds": 0,
        "fee": 0.0,
        "slippage": 0.0,
        "funding_rate_8h": 0.0,
        "vol_gate": False,
        "ema_align_gate": False,
        "time_weighted_exit": False,
        "regime_kelly": False,
        "hurst_gate": False,
        "monthly_trend_filter": False,
        "max_adverse_r": 0.0,
    }
    values.update(overrides)
    return MLTrendParams(**values)


def _bars(rows):
    return pd.DataFrame(
        [
            {
                "open_time": index * BAR_MS,
                "atr": 1.0,
                "long_prob": 0.3,
                "short_prob": 0.3,
                **row,
            }
            for index, row in enumerate(rows)
        ]
    )


def test_entry_and_ml_exit_execute_at_the_following_open():
    bars = _bars(
        [
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 112.0,
                "high": 114.0,
                "low": 110.0,
                "close": 113.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 113.0,
                "high": 114.0,
                "low": 112.0,
                "close": 113.0,
                "long_prob": 0.2,
                "short_prob": 0.2,
            },
            {"open": 107.0, "high": 108.0, "low": 106.0, "close": 107.0},
        ]
    )

    trades = simulate_ml_trend(bars, _params())

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_time == 2 * BAR_MS
    assert trade.entry_price == 112.0
    assert trade.exit_time == 4 * BAR_MS
    assert trade.exit_price == 107.0
    assert trade.reason == "ml_exit"


def test_last_bar_exit_signal_is_not_fillable_but_position_is_forced_closed():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "long_prob": 0.2,
                "short_prob": 0.2,
            },
        ]
    )

    trades = simulate_ml_trend(bars, _params())

    assert len(trades) == 1
    assert trades[0].reason == "end"
    assert trades[0].exit_time == 2 * BAR_MS
    assert trades[0].exit_price == 101.0


def test_last_bar_entry_signal_is_not_fillable():
    bars = _bars(
        [
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
        ]
    )

    assert simulate_ml_trend(bars, _params()) == []


def test_add_signal_executes_at_the_following_open():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 105.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 110.0,
                "high": 111.0,
                "low": 109.0,
                "close": 110.0,
                "long_prob": 0.2,
                "short_prob": 0.2,
            },
            {"open": 108.0, "high": 109.0, "low": 107.0, "close": 108.0},
        ]
    )

    trades = simulate_ml_trend(
        bars,
        _params(max_adds=1, add_atr_dist=2.0, atr_trail=100.0),
    )

    assert len(trades) == 1
    assert trades[0].adds == 1
    assert trades[0].avg_price == pytest.approx(103.3333333333)
    assert trades[0].final_qty == pytest.approx(2.25)


def test_end_of_backtest_force_close_charges_both_sides():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
        ]
    )

    trades = simulate_ml_trend(
        bars,
        _params(fee=0.001, slippage=0.0),
    )

    assert len(trades) == 1
    assert trades[0].reason == "end"
    assert trades[0].pnl_r < 0


def test_ml_reversal_executes_at_the_following_open():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "long_prob": 0.8,
                "short_prob": 0.9,
            },
            {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0},
        ]
    )

    trades = simulate_ml_trend(bars, _params())

    assert len(trades) == 1
    assert trades[0].reason == "ml_reversal"
    assert trades[0].exit_time == 2 * BAR_MS
    assert trades[0].exit_price == 95.0


@pytest.mark.parametrize(
    ("signal", "entry_bar", "gap_bar", "expected_exit"),
    [
        (
            {"long_prob": 0.8, "short_prob": 0.1},
            {"open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0},
            {"open": 80.0, "high": 85.0, "low": 75.0, "close": 82.0},
            80.0,
        ),
        (
            {"long_prob": 0.1, "short_prob": 0.8},
            {"open": 100.0, "high": 105.0, "low": 95.0, "close": 98.0},
            {"open": 120.0, "high": 125.0, "low": 115.0, "close": 118.0},
            120.0,
        ),
    ],
)
def test_gap_stop_never_fills_better_than_the_open(
    signal, entry_bar, gap_bar, expected_exit
):
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr": 10.0,
                **signal,
            },
            {"atr": 10.0, **signal, **entry_bar},
            {"atr": 10.0, **signal, **gap_bar},
        ]
    )

    trades = simulate_ml_trend(
        bars,
        _params(initial_stop_mult=1.0, atr_trail=100.0),
    )

    assert len(trades) == 1
    assert trades[0].reason == "stop"
    assert trades[0].exit_time == 2 * BAR_MS
    assert trades[0].exit_price == expected_exit


def test_entry_bar_can_only_stop_on_ohlc_after_the_open_fill():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr": 10.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 100.0,
                "high": 101.0,
                "low": 89.0,
                "close": 95.0,
                "atr": 10.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
        ]
    )

    trades = simulate_ml_trend(
        bars,
        _params(initial_stop_mult=1.0, atr_trail=100.0),
    )

    assert len(trades) == 1
    assert trades[0].entry_time == BAR_MS
    assert trades[0].entry_price == 100.0
    assert trades[0].exit_time == BAR_MS
    assert trades[0].exit_price == 90.0
    assert trades[0].reason == "stop"


def test_trailing_stop_uses_post_entry_high_and_respects_the_next_gap_open():
    bars = _bars(
        [
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr": 10.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 100.0,
                "high": 120.0,
                "low": 95.0,
                "close": 118.0,
                "atr": 10.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
            {
                "open": 105.0,
                "high": 106.0,
                "low": 100.0,
                "close": 102.0,
                "atr": 10.0,
                "long_prob": 0.8,
                "short_prob": 0.1,
            },
        ]
    )

    trades = simulate_ml_trend(
        bars,
        _params(initial_stop_mult=1.0, atr_trail=1.0),
    )

    assert len(trades) == 1
    assert trades[0].initial_stop == 90.0
    assert trades[0].reason == "stop"
    assert trades[0].exit_time == 2 * BAR_MS
    assert trades[0].exit_price == 105.0
