import numpy as np
import pandas as pd

from backend.app.services import backtest_portfolio


def test_invvol_uses_only_fixed_history_before_backtest(monkeypatch):
    calls = []

    def fake_load_klines(symbol, interval, start, end):
        calls.append((symbol, interval, start, end))
        scale = 1.0 if symbol == "BTCUSDT" else 2.0
        returns = np.array([0.01, -0.01, 0.02, -0.02]) * scale
        close = 100.0 * np.cumprod(np.r_[1.0, 1.0 + returns])
        return pd.DataFrame({"close": close})

    monkeypatch.setattr(backtest_portfolio, "load_klines", fake_load_klines)

    first = backtest_portfolio._alloc_weights(
        ["BTCUSDT", "ETHUSDT"], "2025-04-01", "2025-05-01", "invvol"
    )
    second = backtest_portfolio._alloc_weights(
        ["BTCUSDT", "ETHUSDT"], "2025-04-01", "2026-01-01", "invvol"
    )

    assert first == second
    assert calls == [
        ("BTCUSDT", "1d", "2025-01-01", "2025-03-31"),
        ("ETHUSDT", "1d", "2025-01-01", "2025-03-31"),
        ("BTCUSDT", "1d", "2025-01-01", "2025-03-31"),
        ("ETHUSDT", "1d", "2025-01-01", "2025-03-31"),
    ]
    assert np.isclose(sum(first.values()), 1.0)
    assert first["BTCUSDT"] > first["ETHUSDT"]


def test_alloc_weights_falls_back_to_equal_for_invalid_inputs(monkeypatch):
    monkeypatch.setattr(
        backtest_portfolio,
        "_realized_vol",
        lambda symbol, start, end=None: 0.1 if symbol == "BTCUSDT" else None,
    )

    expected = {"BTCUSDT": 0.5, "ETHUSDT": 0.5}
    assert backtest_portfolio._alloc_weights([], "2025-01-01", "2025-02-01", "invvol") == {}
    assert (
        backtest_portfolio._alloc_weights(
            ["BTCUSDT", "ETHUSDT"], "2025-01-01", "2025-02-01", "unsupported"
        )
        == expected
    )
    assert (
        backtest_portfolio._alloc_weights(
            ["BTCUSDT", "ETHUSDT"], "2025-01-01", "2025-02-01", "invvol"
        )
        == expected
    )


def test_empty_portfolio_is_stable_and_does_not_run_strategy(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("strategy must not run for an empty portfolio")

    monkeypatch.setattr(backtest_portfolio, "backtest_ml_trend", fail_if_called)

    result = backtest_portfolio.run_portfolio_backtest(
        [], "2025-01-01", "2025-02-01", capital=1234.5, alloc_mode="invvol"
    )

    assert result == {
        "equity_curve": [],
        "per_symbol": {},
        "metrics": {
            "total_return_pct": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "num_trades": 0,
            "final_equity": 1234.5,
        },
    }
