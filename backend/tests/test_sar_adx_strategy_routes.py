import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException
from fastapi import HTTPException
from pydantic import ValidationError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.routes import strategy as strategy_routes
from backend.app.services.exchange_executor import RecoveryRequiredError
from backend.app.services.execution_store import ExecutionStoreError
from backend.app.services.live_strategy_runtime import LiveStrategyRuntimeError


class _Query:
    def __init__(self, settings):
        self.settings = settings

    def first(self):
        return self.settings


class _Db:
    def __init__(self, *, testnet=True, symbol="SOLUSDT"):
        self.settings = SimpleNamespace(testnet=testnet, symbol=symbol)

    def query(self, model):
        assert model is strategy_routes.Settings
        return _Query(self.settings)


class _Engine:
    def __init__(self):
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.hydrate_persisted_status = Mock()
        self.status = {
            "running": True,
            "symbol": "SOLUSDT",
            "network": "testnet",
            "strategy_type": "sar_adx_pyramid",
            "config_version": "sar_adx_v3",
            "paper": False,
            "decision_count": 0,
            "submitted_order_count": 0,
            "filled_order_count": 0,
            "rejected_order_count": 0,
            "unknown_order_count": 0,
        }


def _request(**overrides):
    values = {
        "strategy_type": "sar_adx_pyramid",
        "config_version": "sar_adx_v3",
        "symbol": "SOLUSDT",
        "capital_limit": 250.0,
    }
    values.update(overrides)
    return strategy_routes.EngineStartRequest(**values)


def _install(monkeypatch, *, testnet=True, symbol="SOLUSDT"):
    engine = _Engine()
    monkeypatch.setattr(strategy_routes, "bot_engine", engine)
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(strategy_routes.app_state, "symbol", symbol)
    return engine, _Db(testnet=testnet, symbol=symbol)


def test_start_request_normalizes_symbol_and_rejects_removed_strategy():
    assert _request(symbol="SOLUSDT").symbol == "SOLUSDT"
    assert _request(symbol="solusdt").symbol == "SOLUSDT"
    with pytest.raises(ValidationError):
        _request(strategy_type="ml_trend")


def test_testnet_start_binds_server_network_symbol_and_capital(monkeypatch):
    engine, db = _install(monkeypatch)

    result = asyncio.run(strategy_routes.start_engine(_request(), db))

    engine.start.assert_awaited_once()
    client, config = engine.start.await_args.args
    assert client is strategy_routes.app_state.client
    assert config == {
        "name": "CandleMind Trend Strategy",
        "symbol": "SOLUSDT",
        "interval": "5m",
        "check_interval": 15,
        "strategy_type": "sar_adx_pyramid",
        "config_version": "sar_adx_v3",
        "capital_limit": 250.0,
        "network": "testnet",
    }
    assert result["network"] == "testnet"
    assert result["symbol"] == "SOLUSDT"


@pytest.mark.parametrize(
    ("settings_symbol", "connected_symbol"),
    [("ETHUSDT", "SOLUSDT"), ("SOLUSDT", "ETHUSDT")],
)
def test_start_rejects_symbol_not_bound_to_settings_and_connection(
    monkeypatch, settings_symbol, connected_symbol
):
    engine, db = _install(monkeypatch, symbol=connected_symbol)
    db.settings.symbol = settings_symbol

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request(), db))

    assert raised.value.status_code == 409
    assert "bound" in raised.value.detail
    engine.start.assert_not_awaited()


def test_start_requires_connected_binance_client(monkeypatch):
    engine, db = _install(monkeypatch)
    monkeypatch.setattr(strategy_routes.app_state, "client", None)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request(), db))

    assert raised.value.status_code == 503
    assert raised.value.detail == "Binance is not connected"
    engine.start.assert_not_awaited()


def test_mainnet_start_is_disabled_without_server_authorization(monkeypatch):
    engine, db = _install(monkeypatch, testnet=False)
    monkeypatch.delenv("CANDLEMIND_MAINNET_TRADING_ENABLED", raising=False)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            strategy_routes.start_engine(
                _request(mainnet_confirmation="MAINNET:SOLUSDT"), db
            )
        )

    assert raised.value.status_code == 403
    assert "disabled" in raised.value.detail
    engine.start.assert_not_awaited()


def test_mainnet_start_requires_exact_symbol_confirmation(monkeypatch):
    engine, db = _install(monkeypatch, testnet=False)
    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "true")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            strategy_routes.start_engine(
                _request(mainnet_confirmation="MAINNET:BTCUSDT"), db
            )
        )

    assert raised.value.status_code == 403
    assert "confirmation" in raised.value.detail.lower()
    engine.start.assert_not_awaited()


def test_mainnet_start_passes_authoritative_network_after_dual_confirmation(monkeypatch):
    engine, db = _install(monkeypatch, testnet=False)
    engine.status["network"] = "mainnet"
    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "1")

    result = asyncio.run(
        strategy_routes.start_engine(
            _request(mainnet_confirmation="MAINNET:SOLUSDT"), db
        )
    )

    assert engine.start.await_args.args[1]["network"] == "mainnet"
    assert result["network"] == "mainnet"


@pytest.mark.parametrize(
    "error",
    [
        LiveStrategyRuntimeError("private runtime detail"),
        RecoveryRequiredError("private order detail"),
        ExecutionStoreError("private journal path"),
        ValueError("strategy already running for SOLUSDT"),
    ],
)
def test_start_maps_conflicts_without_exposing_exchange_credentials(monkeypatch, error):
    engine, db = _install(monkeypatch)
    engine.start.side_effect = error

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request(), db))

    assert raised.value.status_code == 409
    assert "api_key" not in raised.value.detail.lower()


@pytest.mark.parametrize(
    "error",
    [
        BinanceRequestException("upstream payload contained secret details"),
        BinanceAPIException(
            response=type(
                "Response",
                (),
                {
                    "status_code": 429,
                    "text": '{"code": -1003, "msg": "sensitive upstream detail"}',
                    "request": None,
                },
            )(),
            status_code=429,
            text='{"code": -1003, "msg": "sensitive upstream detail"}',
        ),
    ],
)
def test_start_maps_binance_response_failures_to_safe_502(monkeypatch, error):
    engine, db = _install(monkeypatch)
    engine.start.side_effect = error

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request(), db))

    assert raised.value.status_code == 502
    assert raised.value.detail == "Binance rejected or returned an invalid response"


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("secret"),
        ConnectionError("secret"),
        RequestsTimeout("secret"),
        RequestsConnectionError("secret"),
    ],
)
def test_start_maps_binance_availability_failures_to_safe_503(monkeypatch, error):
    engine, db = _install(monkeypatch)
    engine.start.side_effect = error

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request(), db))

    assert raised.value.status_code == 503
    assert raised.value.detail == "Binance is temporarily unavailable"


def test_status_hydrates_bound_network_and_returns_execution_fields(monkeypatch):
    engine, db = _install(monkeypatch)

    result = strategy_routes.engine_status(db)

    engine.hydrate_persisted_status.assert_called_once_with("SOLUSDT", "testnet")
    assert result["paper"] is False
    assert result["decision_count"] == 0
    assert result["submitted_order_count"] == 0
    assert result["filled_order_count"] == 0
    assert result["rejected_order_count"] == 0
    assert result["unknown_order_count"] == 0


def test_stop_maps_reconciliation_failure_to_conflict(monkeypatch):
    engine, _db = _install(monkeypatch)
    engine.stop.side_effect = RecoveryRequiredError("unknown stop order")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.stop_engine())

    assert raised.value.status_code == 409
    assert "reconciliation" in raised.value.detail.lower()
