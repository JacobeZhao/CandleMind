import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from backend.app.services import ml_signal
from backend.app.services.bot_engine import (
    BotEngine,
    _base_df,
    _trend_feature_snapshot,
)
from backend.app.services.ml_strategy import MLTrendParams
from backend.app.services.trend_decision import (
    TrendDecisionParams,
    TrendFeatureSnapshot,
    TrendPositionSnapshot,
    decide_entry,
    decide_ml_exit,
)


FEATURE_TS = "2026-07-16T08:00:00Z"


def _runtime_params(**overrides):
    values = {
        "entry_long_threshold": 0.6,
        "entry_short_threshold": 0.6,
        "min_prob_gap": 0.1,
        "min_prob_gap_large_cap": 0.1,
        "initial_stop_mult": 4.0,
        "atr_trail": 100.0,
        "max_adds": 2,
        "add_atr_dist": 1.0,
        "add_size_frac": 0.5,
        "fee": 0.0,
        "vol_gate": False,
        "ema_align_gate": False,
        "hurst_gate": False,
        "monthly_trend_filter": False,
        "time_weighted_exit": False,
        "max_adverse_r": 0.0,
    }
    values.update(overrides)
    return MLTrendParams(**values)


def _signal(**overrides):
    values = {
        "long_prob": 0.8,
        "short_prob": 0.1,
        "model_available": True,
        "feature_fresh": True,
        "feature_timestamp": FEATURE_TS,
        "pos_size_mult": 1.0,
        "drift_warning": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _feature_frame(close=100.0):
    return pd.DataFrame(
        {
            "open_time": [FEATURE_TS],
            "close": [close],
            "5m_vol_regime": [1.0],
            "5m_ema_align_score": [1.0],
            "5m_hurst": [0.9],
        }
    )


def _stub_journal(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "backend.app.services.journal",
        SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )


def _kline_rows(close=100.0, count=60):
    rows = []
    for index in range(count):
        open_time = index * 5 * 60_000
        rows.append(
            [
                open_time,
                str(close),
                str(close + 1.0),
                str(close - 1.0),
                str(close),
                "10",
                open_time + 5 * 60_000 - 1,
                "1000",
                10,
                "5",
                "500",
                "0",
            ]
        )
    return rows


class FakeLiveClient:
    def __init__(self, *, position_amt=0.0, close=100.0):
        self.position_amt = float(position_amt)
        self.rows = _kline_rows(close)
        self.market_orders = []
        self.kline_calls = 0

    def futures_klines(self, **kwargs):
        self.kline_calls += 1
        assert kwargs["symbol"] == "BTCUSDT"
        assert kwargs["interval"] == "5m"
        return self.rows[-kwargs["limit"]:]

    def futures_position_information(self, **_kwargs):
        if self.position_amt == 0:
            return []
        return [{"positionAmt": str(self.position_amt)}]

    def futures_account_balance(self):
        return [{"asset": "USDT", "availableBalance": "10000"}]

    def futures_create_order(self, **kwargs):
        self.market_orders.append(kwargs)
        quantity = float(kwargs["quantity"])
        self.position_amt += quantity if kwargs["side"] == "BUY" else -quantity
        return {"orderId": len(self.market_orders)}


@pytest.mark.parametrize(
    ("model_available", "feature_fresh", "reason"),
    [
        (False, True, "model_unavailable"),
        (True, False, "feature_stale"),
    ],
)
def test_unusable_ml_snapshot_blocks_entry_exit_and_reversal(
    model_available, feature_fresh, reason
):
    snapshot = TrendFeatureSnapshot(
        long_prob=0.0,
        short_prob=1.0,
        close=100.0,
        model_available=model_available,
        feature_fresh=feature_fresh,
    )
    params = TrendDecisionParams(
        vol_gate=False,
        ema_align_gate=False,
        hurst_gate=False,
        monthly_trend_filter=False,
        time_weighted_exit=False,
    )

    entry = decide_entry("BTCUSDT", snapshot, params)
    exit_intent = decide_ml_exit(
        snapshot,
        TrendPositionSnapshot(direction=1, bars_held=20),
        params,
    )

    assert (entry.action, entry.reason_code) == ("hold", reason)
    assert (exit_intent.action, exit_intent.reason_code) == ("hold", reason)


def test_base_df_uses_futures_klines_and_normalizes_runtime_bars():
    client = FakeLiveClient()

    frame = _base_df(client, "BTCUSDT", "5m", limit=20)

    assert client.kline_calls == 1
    assert len(frame) == 20
    assert pd.api.types.is_float_dtype(frame["close"])
    assert pd.api.types.is_datetime64_any_dtype(frame["open_time"])


def test_live_cycle_uses_shared_execution_semantics_and_deduplicates(monkeypatch):
    client = FakeLiveClient()
    signal = _signal()
    params = _runtime_params()
    monkeypatch.setattr(ml_signal, "get_ml_signal", lambda *_args: signal)
    monkeypatch.setattr(ml_signal, "_latest_features", lambda *_args: _feature_frame())
    monkeypatch.setattr(
        MLTrendParams,
        "from_runtime_config",
        classmethod(lambda cls, *_args, **_kwargs: params),
    )

    engine = BotEngine()
    engine.paper = False
    engine._get_filters = lambda *_args: (0.001, 0.1)
    engine._extreme_vol = lambda *_args: False
    engine._check_circuit = lambda *_args: False
    engine._place_close = AsyncMock(return_value=77)
    config = {"strategy_params": {"entry_interval": "5m"}}

    asyncio.run(
        engine._cycle(client, "BTCUSDT", "5m", "ml_trend", config, 0.01)
    )
    first_trade = dict(engine._open_trade)
    asyncio.run(
        engine._cycle(client, "BTCUSDT", "5m", "ml_trend", config, 0.01)
    )

    assert client.kline_calls == 2
    assert len(client.market_orders) == 1
    assert engine._place_close.await_count == 1
    assert first_trade["adds"] == 0
    assert first_trade["stop_price"] == pytest.approx(92.0)
    assert first_trade["add_qty"] == pytest.approx(first_trade["tranche"] * 0.5)
    assert engine._open_trade == first_trade
    assert "duplicate_feature" in engine.last_action


def test_live_entry_is_flattened_and_engine_halted_when_stop_fails(monkeypatch):
    client = FakeLiveClient()
    signal = _signal()
    params = _runtime_params()
    monkeypatch.setattr(ml_signal, "get_ml_signal", lambda *_args: signal)
    monkeypatch.setattr(ml_signal, "_latest_features", lambda *_args: _feature_frame())
    monkeypatch.setattr(
        MLTrendParams,
        "from_runtime_config",
        classmethod(lambda cls, *_args, **_kwargs: params),
    )

    engine = BotEngine()
    engine.running = True
    engine.paper = False
    engine._get_filters = lambda *_args: (0.001, 0.1)
    engine._extreme_vol = lambda *_args: False
    engine._check_circuit = lambda *_args: False
    engine._place_close = AsyncMock(side_effect=RuntimeError("stop rejected"))

    asyncio.run(
        engine._cycle(
            client,
            "BTCUSDT",
            "5m",
            "ml_trend",
            {"strategy_params": {"entry_interval": "5m"}},
            0.01,
        )
    )

    assert len(client.market_orders) == 2
    assert client.market_orders[1]["reduceOnly"] is True
    assert client.position_amt == pytest.approx(0.0)
    assert engine._open_trade is None
    assert engine.running is False
    assert "entry was closed" in engine.error_msg


@pytest.mark.parametrize(
    ("signal_overrides", "expected_reason"),
    [
        ({"model_available": False}, "model_unavailable"),
        ({"feature_fresh": False}, "feature_stale"),
    ],
)
def test_live_stale_position_keeps_existing_exchange_stop(
    monkeypatch, signal_overrides, expected_reason
):
    client = FakeLiveClient(position_amt=2.0, close=120.0)
    signal = _signal(long_prob=0.0, short_prob=1.0, **signal_overrides)
    params = _runtime_params()
    monkeypatch.setattr(ml_signal, "get_ml_signal", lambda *_args: signal)
    monkeypatch.setattr(ml_signal, "_latest_features", lambda *_args: _feature_frame(120.0))
    monkeypatch.setattr(
        MLTrendParams,
        "from_runtime_config",
        classmethod(lambda cls, *_args, **_kwargs: params),
    )

    engine = BotEngine()
    engine.paper = False
    engine._open_trade = {
        "mode": "ml_trend",
        "dir": 1,
        "adds": 0,
        "max_adds": 2,
        "tranche": 2.0,
        "add_size_frac": 0.5,
        "step_size": 0.001,
        "stop_price": 90.0,
        "stop_id": 44,
        "peak": 100.0,
        "last_add_ref": 100.0,
        "atr_trail": 2.0,
        "avg": 100.0,
        "entry_feature_timestamp": "2026-07-16T07:55:00Z",
    }
    before = dict(engine._open_trade)
    engine._replace_stop = AsyncMock()
    engine._close_all = AsyncMock()

    asyncio.run(
        engine._cycle(
            client,
            "BTCUSDT",
            "5m",
            "ml_trend",
            {"strategy_params": {}},
            0.01,
        )
    )

    assert engine._open_trade == before
    assert client.market_orders == []
    engine._replace_stop.assert_not_awaited()
    engine._close_all.assert_not_awaited()
    assert expected_reason in engine.last_action


def test_paper_stale_position_blocks_ml_actions_but_keeps_risk_stop(monkeypatch):
    _stub_journal(monkeypatch)
    engine = BotEngine()
    params = _runtime_params()
    signal = _signal(
        long_prob=0.0,
        short_prob=1.0,
        feature_fresh=False,
    )
    snapshot = _trend_feature_snapshot(signal, _feature_frame(120.0), params, 120.0)
    engine._paper_pos = {
        "mode": "ml_trend",
        "dir": 1,
        "avg": 100.0,
        "qty": 2.0,
        "tranche": 2.0,
        "add_size_frac": 0.5,
        "stop": 90.0,
        "peak": 100.0,
        "last_add_ref": 100.0,
        "adds": 0,
        "max_adds": 2,
        "decision_params": params.to_decision_params(),
        "entry_feature_timestamp": "2026-07-16T07:55:00Z",
    }
    before = dict(engine._paper_pos)

    engine._ml_trend_paper_step(
        "BTCUSDT",
        {},
        signal,
        params,
        snapshot,
        120.0,
        2.0,
        0.01,
        allow_ml_decision=False,
        decision_reason="feature_stale",
    )
    assert engine._paper_pos == before

    engine._ml_trend_paper_step(
        "BTCUSDT",
        {},
        signal,
        params,
        snapshot,
        85.0,
        2.0,
        0.01,
        allow_ml_decision=False,
        decision_reason="feature_stale",
    )
    assert engine._paper_pos is None


def test_paper_adds_count_only_new_tranches_and_decay_from_initial(monkeypatch):
    _stub_journal(monkeypatch)
    engine = BotEngine()
    engine._extreme_vol = lambda *_args: False
    engine._check_circuit = lambda *_args: False
    params = _runtime_params()
    signal = _signal()
    snapshot = _trend_feature_snapshot(signal, _feature_frame(), params, 100.0)

    engine._ml_trend_paper_step(
        "BTCUSDT", {}, signal, params, snapshot, 100.0, 2.0, 0.01
    )
    initial_qty = engine._paper_pos["qty"]
    assert engine._paper_pos["adds"] == 0
    assert engine._paper_pos["stop"] == pytest.approx(92.0)

    next_signal = _signal(feature_timestamp="2026-07-16T08:05:00Z")
    next_snapshot = _trend_feature_snapshot(
        next_signal, _feature_frame(103.0), params, 103.0
    )
    engine._ml_trend_paper_step(
        "BTCUSDT", {}, next_signal, params, next_snapshot, 103.0, 2.0, 0.01
    )

    assert engine._paper_pos["adds"] == 1
    assert engine._paper_pos["qty"] == pytest.approx(initial_qty * 1.5)
