import asyncio

import pandas as pd
import pytest

from backend.app.routes import backtest as backtest_route


@pytest.mark.parametrize(
    ("slippage", "slippage_bps", "expected_rate"),
    [
        (None, 13.0, 0.0013),
        (0.0021, 99.0, 0.0021),
    ],
)
def test_generic_route_passes_cost_and_execution_parameters(
    monkeypatch,
    slippage,
    slippage_bps,
    expected_rate,
):
    captured = {}
    frame = pd.DataFrame({"close": [100.0] * 20})

    def fake_run_backtest(df, strategy_type, strategy_params, **kwargs):
        captured.update(kwargs)
        assert df is frame
        assert strategy_type == "ema_cross"
        assert strategy_params == {"fast": 3, "slow": 8}
        return {"trades": [], "equity_curve": [], "metrics": {}}

    monkeypatch.setattr(backtest_route.app_state, "client", object())
    monkeypatch.setattr(backtest_route, "fetch_historical", lambda *args: frame)
    monkeypatch.setattr(backtest_route, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(backtest_route, "_save_backtest", lambda *args: None)

    request = backtest_route.RunRequest(
        symbol="ETHUSDT",
        interval="1h",
        start_date="2026-01-01",
        end_date="2026-02-01",
        strategy_type="ema_cross",
        strategy_params={"fast": 3, "slow": 8},
        initial_capital=12_345,
        leverage=5,
        risk_pct=0.02,
        sl_pct=0.03,
        tp_pct=0.07,
        taker_fee=0.0007,
        maker_fee=0.0001,
        slippage=slippage,
        slippage_bps=slippage_bps,
        fee_mult=2.5,
        use_funding=False,
        funding_rate=-0.0003,
        exec_mode="limit",
        model_liq=True,
    )

    result = asyncio.run(backtest_route.backtest_run(request, db=object()))

    assert result["metrics"] == {}
    assert captured == {
        "initial_capital": 12_345,
        "leverage": 5,
        "risk_pct": 0.02,
        "sl_pct": 0.03,
        "tp_pct": 0.07,
        "taker_fee": 0.0007,
        "maker_fee": 0.0001,
        "slippage_rate": pytest.approx(expected_rate),
        "fee_mult": 2.5,
        "use_funding": False,
        "funding_rate": -0.0003,
        "exec_mode": "limit",
        "model_liq": True,
    }
