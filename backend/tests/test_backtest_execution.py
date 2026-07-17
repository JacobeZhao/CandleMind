import pandas as pd
import pytest

from backend.app.services import backtest


def _frame(rows):
    times = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(
        [
            {
                "open_time": times[index],
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": 1.0,
            }
            for index, (open_price, high_price, low_price, close_price)
            in enumerate(rows)
        ]
    )


@pytest.fixture
def deterministic_strategy(monkeypatch):
    def signals(frame, params):
        return pd.Series(params["signals"], index=frame.index)

    monkeypatch.setitem(backtest._SIGNAL_FNS, "deterministic", (signals, []))


def _run(rows, signals, **overrides):
    kwargs = {
        "initial_capital": 10_000,
        "risk_pct": 0.01,
        "sl_pct": 0.5,
        "tp_pct": 1.0,
        "fee_rate": 0.0,
        "slippage_rate": 0.0,
    }
    kwargs.update(overrides)
    return backtest.run_backtest(
        _frame(rows),
        "deterministic",
        {"signals": signals, "swing_lookback": 10},
        **kwargs,
    )


def test_zero_signals_preserve_capital_and_mark_every_bar(deterministic_strategy):
    result = _run(
        [(100, 101, 99, 100), (100, 102, 98, 101), (101, 103, 100, 102)],
        [0, 0, 0],
    )

    assert result["trades"] == []
    assert [point["equity"] for point in result["equity_curve"]] == [10_000] * 3
    assert result["metrics"]["num_trades"] == 0
    assert result["metrics"]["total_return"] == 0
    assert result["metrics"]["total_cost"] == 0


def test_signal_executes_at_next_open_even_when_market_gaps(deterministic_strategy):
    result = _run(
        [(100, 100, 100, 100), (120, 120, 120, 120), (120, 120, 120, 120)],
        [1, 0, 0],
    )

    trade = result["trades"][0]
    assert trade["entry_time"] == result["equity_curve"][1]["time"]
    assert trade["entry_price"] == 120
    assert trade["entry_price"] != 100


def test_flat_round_trip_deducts_both_fees_and_both_slippage_legs(
    deterministic_strategy,
):
    result = _run(
        [(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 100)],
        [1, 0, 0],
        sl_pct=0.01,
        tp_pct=0.02,
        fee_rate=0.001,
        slippage_rate=0.002,
    )

    trade = result["trades"][0]
    assert trade["entry_price"] == pytest.approx(100.2)
    assert trade["exit_price"] == pytest.approx(99.8)
    assert trade["fees"] == pytest.approx(20.0)
    assert trade["slippage_cost"] == pytest.approx(40.0)
    assert trade["total_cost"] == pytest.approx(60.0)
    assert trade["pnl"] == pytest.approx(-60.0)
    assert result["metrics"]["total_return"] == pytest.approx(-60.0)
    assert result["metrics"]["total_cost"] == pytest.approx(60.0)


def test_legacy_positional_call_uses_nonzero_default_one_way_costs(
    deterministic_strategy,
):
    result = backtest.run_backtest(
        _frame([(100, 100, 100, 100)] * 3),
        "deterministic",
        {"signals": [1, 0, 0], "swing_lookback": 10},
        10_000,
        1,
        0.01,
        0.01,
        0.02,
    )

    trade = result["trades"][0]
    assert trade["fees"] == pytest.approx(8.0)
    assert trade["slippage_cost"] == pytest.approx(10.0)
    assert trade["pnl"] == pytest.approx(-18.0)


def test_unrealized_loss_is_included_in_drawdown_before_recovery(
    deterministic_strategy,
):
    results = [
        _run(
            [(100, 100, 100, 100), (100, 100, 80, 80), (80, 100, 80, 100)],
            [1, 0, 0],
            leverage=leverage,
        )
        for leverage in (1, 5)
    ]

    for result in results:
        assert [point["equity"] for point in result["equity_curve"]] == [
            10_000,
            9_960,
            10_000,
        ]
        assert result["metrics"]["total_return"] == 0
        assert result["metrics"]["max_drawdown"] == pytest.approx(0.4)


def test_risk_sized_stop_loss_is_identical_at_1x_and_5x(
    deterministic_strategy,
):
    results = [
        _run(
            [(100, 100, 100, 100), (100, 100, 100, 100), (99, 99, 99, 99)],
            [1, 0, 0],
            leverage=leverage,
            sl_pct=0.01,
            tp_pct=0.02,
        )
        for leverage in (1, 5)
    ]

    one_x, five_x = (result["trades"][0] for result in results)
    assert one_x["qty"] == five_x["qty"] == pytest.approx(100.0)
    assert one_x["gross_pnl"] == five_x["gross_pnl"] == pytest.approx(-100.0)
    assert one_x["pnl"] == five_x["pnl"] == pytest.approx(-100.0)
    assert results[0]["metrics"] == results[1]["metrics"]


def test_leverage_only_increases_the_maximum_order_quantity(
    deterministic_strategy,
):
    results = [
        _run(
            [(100, 100, 100, 100)] * 3,
            [1, 0, 0],
            leverage=leverage,
            risk_pct=0.10,
            sl_pct=0.001,
            tp_pct=1.0,
        )
        for leverage in (1, 5)
    ]

    assert results[0]["trades"][0]["qty"] == pytest.approx(100.0)
    assert results[1]["trades"][0]["qty"] == pytest.approx(500.0)


@pytest.mark.parametrize(
    ("signal", "bar_count", "expected_funding", "expected_pnl"),
    [
        (1, 10, 2.0, -2.0),
        (-1, 10, -2.0, 2.0),
        (1, 6, 1.0, -1.0),
    ],
)
def test_funding_uses_position_direction_and_holding_hours(
    deterministic_strategy,
    signal,
    bar_count,
    expected_funding,
    expected_pnl,
):
    result = _run(
        [(100, 100, 100, 100)] * bar_count,
        [signal] + [0] * (bar_count - 1),
        funding_rate=0.01,
        use_funding=True,
    )

    trade = result["trades"][0]
    expected_hours = bar_count - 2
    assert trade["holding_hours"] == pytest.approx(expected_hours)
    assert trade["funding_cost"] == pytest.approx(expected_funding)
    assert trade["pnl"] == pytest.approx(expected_pnl)
    assert result["metrics"]["total_funding"] == pytest.approx(expected_funding)


def test_unverifiable_maker_mode_uses_taker_fee_and_is_disclosed(
    deterministic_strategy,
):
    result = _run(
        [(100, 100, 100, 100)] * 3,
        [1, 0, 0],
        sl_pct=0.01,
        taker_fee=0.001,
        maker_fee=0.0,
        fee_mult=2.0,
        exec_mode="limit",
    )

    assert result["trades"][0]["fees"] == pytest.approx(40.0)
    assert result["execution"]["requested_mode"] == "limit"
    assert result["execution"]["fee_liquidity"] == "taker"
    assert result["execution"]["maker_fee_applied"] is False
    assert "taker fees are applied" in result["execution"]["maker_fallback_reason"]


def test_open_position_is_liquidated_at_last_close_with_exit_cost(
    deterministic_strategy,
):
    result = _run(
        [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 100, 110)],
        [1, 0, 1],
        fee_rate=0.001,
    )

    trade = result["trades"][0]
    assert len(result["trades"]) == 1
    assert trade["exit_reason"] == "期末强平"
    assert trade["exit_price"] == 110
    assert trade["exit_fee"] > 0
    assert result["equity_curve"][-1]["equity"] == pytest.approx(10_019.58)


def test_reversal_closes_then_opens_and_charges_all_four_fee_legs(
    deterministic_strategy,
):
    result = _run(
        [
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (100, 100, 100, 100),
        ],
        [1, -1, 0, 0],
        fee_rate=0.001,
    )

    first, second = result["trades"]
    assert first["exit_reason"] == "反转"
    assert first["exit_time"] == second["entry_time"]
    assert first["entry_fee"] > 0 and first["exit_fee"] > 0
    assert second["entry_fee"] > 0 and second["exit_fee"] > 0
    assert result["metrics"]["total_fees"] == pytest.approx(
        first["fees"] + second["fees"], abs=1e-4
    )


@pytest.mark.parametrize(
    ("signal", "rows", "expected_side", "expected_exit"),
    [
        (
            1,
            [(100, 100, 100, 100), (100, 100, 99, 100), (90, 91, 89, 90)],
            "LONG",
            90,
        ),
        (
            -1,
            [(100, 100, 100, 100), (100, 101, 100, 100), (110, 111, 109, 110)],
            "SHORT",
            110,
        ),
    ],
)
def test_gap_stop_fills_at_open_not_at_better_stop_price(
    deterministic_strategy,
    signal,
    rows,
    expected_side,
    expected_exit,
):
    result = _run(
        rows,
        [signal, 0, 0],
        sl_pct=0.015,
        tp_pct=0.03,
    )

    trade = result["trades"][0]
    assert trade["side"] == expected_side
    assert trade["exit_reason"] == "止损"
    assert trade["exit_price"] == expected_exit
