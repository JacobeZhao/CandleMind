import asyncio
from unittest.mock import AsyncMock

import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException
from fastapi import HTTPException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.routes import strategy as strategy_routes
from backend.app.services import bot_engine as bot_engine_module
from backend.app.services.bot_engine import BotEngine
from backend.app.services.sar_adx_runtime import SarAdxRuntimeError


def _engine_config(**overrides):
    values = {
        "name": "SAR + ADX Pyramid V3",
        "symbol": "SOLUSDT",
        "interval": "5m",
        "strategy_type": "sar_adx_pyramid",
        "config_version": "sar_adx_v3",
        "paper": True,
        "initial_capital": 10_000.0,
        "check_interval": 3600,
    }
    values.update(overrides)
    return values


class _ReadOnlyClient:
    allowed_methods = {
        "futures_exchange_info",
        "futures_funding_rate",
        "futures_klines",
        "futures_mark_price",
        "futures_time",
    }

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name not in self.allowed_methods:
            raise AssertionError(f"unexpected Binance client access: {name}")

        def call(**_kwargs):
            self.calls.append(name)
            if name == "futures_exchange_info":
                return {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING"}]}
            if name == "futures_klines":
                return [[0, "100", "101", "99", "100", "1", 299999,
                         "100", 1, "0.5", "50", "0"]]
            if name == "futures_time":
                return {"serverTime": 0}
            if name == "futures_funding_rate":
                return []
            if name == "futures_mark_price":
                return {"markPrice": "100"}
            raise AssertionError(name)

        return call


def _install_runtime(monkeypatch, runtime):
    monkeypatch.setattr(
        "backend.app.services.bot_engine.SarAdxPaperRuntime",
        lambda *_args, **_kwargs: runtime,
    )


def _assert_start_rolled_back(engine):
    assert engine.running is False
    assert engine._task is None
    assert engine._sar_adx_runtime is None
    assert engine._symbol == ""
    assert engine._strategy_name == ""
    assert engine._strategy_type == ""
    assert engine._config_version == ""


def _request(**overrides):
    values = {
        "strategy_type": "sar_adx_pyramid",
        "config_version": "sar_adx_v3",
        "symbol": "ETHUSDT",
        "paper": True,
        "initial_capital": 10_000.0,
    }
    values.update(overrides)
    return strategy_routes.EngineStartRequest(**values)


def test_start_contract_is_paper_only():
    with pytest.raises(HTTPException, match="paper trading"):
        asyncio.run(strategy_routes.start_engine(_request(paper=False)))


def test_start_contract_rejects_removed_strategy():
    with pytest.raises(ValueError):
        _request(strategy_type="ml_trend")


def test_start_requires_connected_binance_client(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(strategy_routes.app_state, "client", None)
    monkeypatch.setattr(strategy_routes.bot_engine, "start", start)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request()))

    assert raised.value.status_code == 503
    assert raised.value.detail == "Binance is not connected"
    start.assert_not_awaited()


def test_start_binds_explicit_symbol(monkeypatch):
    captured = {}

    async def start(_client, config):
        captured.update(config)
        strategy_routes.bot_engine._symbol = config["symbol"]
        strategy_routes.bot_engine._strategy_name = config["strategy_type"]
        strategy_routes.bot_engine._config_version = config["config_version"]

    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(strategy_routes.bot_engine, "start", start)
    result = asyncio.run(strategy_routes.start_engine(_request()))

    assert set(captured) == {
        "name",
        "symbol",
        "interval",
        "check_interval",
        "strategy_type",
        "paper",
        "config_version",
        "initial_capital",
    }
    assert captured["symbol"] == "ETHUSDT"
    assert captured["strategy_type"] == "sar_adx_pyramid"
    assert captured["paper"] is True
    assert result["symbol"] == "ETHUSDT"


def test_start_preserves_running_conflict(monkeypatch):
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(
        strategy_routes.bot_engine,
        "start",
        AsyncMock(side_effect=ValueError("strategy already running for SOLUSDT")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request()))

    assert raised.value.status_code == 409
    assert raised.value.detail == "strategy already running for SOLUSDT"


def test_start_maps_paper_recovery_failure_to_conflict(monkeypatch):
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(
        strategy_routes.bot_engine,
        "start",
        AsyncMock(side_effect=SarAdxRuntimeError("missed execution open")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request()))

    assert raised.value.status_code == 409
    assert "requires recovery" in raised.value.detail


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
            text='{ "code": -1003, "msg": "sensitive upstream detail" }',
        ),
    ],
)
def test_start_maps_binance_response_failures_to_safe_502(monkeypatch, error):
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(
        strategy_routes.bot_engine, "start", AsyncMock(side_effect=error)
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request()))

    assert raised.value.status_code == 502
    assert raised.value.detail == "Binance rejected or returned an invalid response"
    assert "sensitive" not in raised.value.detail


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("sensitive timeout detail"),
        ConnectionError("sensitive connection detail"),
        RequestsTimeout("sensitive request timeout detail"),
        RequestsConnectionError("sensitive request connection detail"),
    ],
)
def test_start_maps_binance_availability_failures_to_safe_503(monkeypatch, error):
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(
        strategy_routes.bot_engine, "start", AsyncMock(side_effect=error)
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.start_engine(_request()))

    assert raised.value.status_code == 503
    assert raised.value.detail == "Binance is temporarily unavailable"
    assert "sensitive" not in raised.value.detail


def test_start_does_not_hide_programming_errors(monkeypatch):
    monkeypatch.setattr(strategy_routes.app_state, "client", object())
    monkeypatch.setattr(
        strategy_routes.bot_engine,
        "start",
        AsyncMock(side_effect=RuntimeError("runtime invariant failed")),
    )

    with pytest.raises(RuntimeError, match="runtime invariant failed"):
        asyncio.run(strategy_routes.start_engine(_request()))


def test_engine_rejects_running_symbol_change():
    engine = BotEngine()

    async def scenario():
        engine.running = True
        engine._symbol = "SOLUSDT"
        engine._task = asyncio.create_task(asyncio.Event().wait())
        try:
            with pytest.raises(ValueError, match="already running"):
                await engine.start(object(), _engine_config(symbol="BTCUSDT"))
        finally:
            await engine.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"initial_capital": 20_000.0},
        {"check_interval": 30.0},
    ],
)
def test_engine_rejects_running_configuration_change(override):
    engine = BotEngine()

    async def scenario():
        engine.running = True
        engine._symbol = "SOLUSDT"
        engine._strategy_type = "sar_adx_pyramid"
        engine._config_version = "sar_adx_v3"
        engine._paper_cap = 10_000.0
        engine._check_interval = 15.0
        engine._task = asyncio.create_task(asyncio.Event().wait())
        try:
            config = _engine_config(check_interval=15.0)
            config.update(override)
            with pytest.raises(ValueError, match="already running"):
                await engine.start(object(), config)
        finally:
            await engine.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"strategy_type": "ml_trend"},
        {"config_version": "sar_adx_v2"},
        {"paper": False, "live_authorized": True},
        {"interval": "15m"},
    ],
)
def test_engine_rejects_any_start_outside_exact_sar_adx_tuple(override):
    engine = BotEngine()

    class Client:
        def __getattr__(self, name):
            raise AssertionError(f"invalid start accessed Binance client: {name}")

    with pytest.raises(ValueError):
        asyncio.run(engine.start(Client(), _engine_config(**override)))

    _assert_start_rolled_back(engine)


@pytest.mark.parametrize("field", ["initial_capital", "check_interval"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_engine_rejects_non_finite_numeric_config_before_client_access(field, value):
    engine = BotEngine()

    class Client:
        def __getattr__(self, name):
            raise AssertionError(f"invalid start accessed Binance client: {name}")

    with pytest.raises(ValueError, match="must be positive"):
        asyncio.run(engine.start(Client(), _engine_config(**{field: value})))

    _assert_start_rolled_back(engine)


def test_engine_status_identifies_sar_adx_runtime():
    engine = BotEngine()
    engine._strategy_name = "sar_adx_pyramid"
    engine._strategy_type = "sar_adx_pyramid"
    engine._symbol = "SOLUSDT"
    engine._config_version = "sar_adx_v3"
    status = engine.status
    assert status["strategy_type"] == "sar_adx_pyramid"
    assert status["symbol"] == "SOLUSDT"
    assert status["config_version"] == "sar_adx_v3"


def test_sar_adx_cycle_never_calls_exchange_trading_methods(monkeypatch, tmp_path):
    from backend.app.services.sar_adx_state_store import SarAdxStateStore

    class Client:
        def futures_exchange_info(self):
            return {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING"}]}

        def futures_change_leverage(self, **_kwargs):
            raise AssertionError("paper runtime changed leverage")

        def futures_change_margin_type(self, **_kwargs):
            raise AssertionError("paper runtime changed margin type")

        def futures_create_order(self, **_kwargs):
            raise AssertionError("paper runtime placed a real order")

    engine = BotEngine()
    monkeypatch.setattr(
        engine,
        "_cycle",
        AsyncMock(return_value=bot_engine_module._CycleResult(100.0, "NONE", "", 0)),
    )
    monkeypatch.setattr(
        "backend.app.services.sar_adx_runtime.SarAdxStateStore",
        lambda: SarAdxStateStore(tmp_path),
    )
    asyncio.run(
        engine.start(
            Client(),
            {
                "symbol": "SOLUSDT",
                "interval": "5m",
                "strategy_type": "sar_adx_pyramid",
                "config_version": "sar_adx_v3",
                "paper": True,
                "check_interval": 3600,
            },
        )
    )
    asyncio.run(engine.stop())


def test_sar_adx_client_surface_is_strictly_read_only(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()

    class Runtime:
        def process_bars(self, *_args, **_kwargs):
            return []

        def status(self, _mark_price):
            return {"direction": 0, "last_processed_bar": None}

    runtime = Runtime()
    _install_runtime(monkeypatch, runtime)

    async def scenario():
        await engine.start(client, _engine_config())
        await engine.stop()

    asyncio.run(scenario())

    assert set(client.calls) == client.allowed_methods


def test_concurrent_starts_commit_only_one_runtime(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    runtime = object()
    warmup_entered = asyncio.Event()
    release_warmup = asyncio.Event()
    warmup_count = 0
    _install_runtime(monkeypatch, runtime)

    async def cycle(*_args, **_kwargs):
        nonlocal warmup_count
        warmup_count += 1
        warmup_entered.set()
        await release_warmup.wait()
        return bot_engine_module._CycleResult(100.0, "NONE", "warm", 0)

    async def loop(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "_cycle", cycle)
    monkeypatch.setattr(engine, "_loop", loop)

    async def scenario():
        first = asyncio.create_task(engine.start(client, _engine_config()))
        await warmup_entered.wait()
        second = asyncio.create_task(engine.start(client, _engine_config()))
        await asyncio.sleep(0)
        release_warmup.set()
        await asyncio.gather(first, second)
        assert engine.running is True
        assert engine._sar_adx_runtime is runtime
        assert warmup_count == 1
        await engine.stop()

    asyncio.run(scenario())


def test_stop_during_warmup_waits_for_start_then_stops(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    warmup_entered = asyncio.Event()
    release_warmup = asyncio.Event()
    _install_runtime(monkeypatch, object())

    async def cycle(*_args, **_kwargs):
        warmup_entered.set()
        await release_warmup.wait()
        return bot_engine_module._CycleResult(100.0, "NONE", "warm", 0)

    async def loop(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "_cycle", cycle)
    monkeypatch.setattr(engine, "_loop", loop)

    async def scenario():
        start = asyncio.create_task(engine.start(client, _engine_config()))
        await warmup_entered.wait()
        stop = asyncio.create_task(engine.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        release_warmup.set()
        await asyncio.gather(start, stop)
        assert engine.running is False
        assert engine._task is None

    asyncio.run(scenario())


def test_warmup_failure_rolls_back_all_staged_state(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    _install_runtime(monkeypatch, object())
    monkeypatch.setattr(
        engine, "_cycle", AsyncMock(side_effect=RuntimeError("warmup failed"))
    )

    with pytest.raises(RuntimeError, match="warmup failed"):
        asyncio.run(engine.start(client, _engine_config()))

    _assert_start_rolled_back(engine)


def test_loop_failure_is_terminal_and_engine_can_restart(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    runtimes = iter((object(), object()))
    monkeypatch.setattr(
        "backend.app.services.bot_engine.SarAdxPaperRuntime",
        lambda *_args, **_kwargs: next(runtimes),
    )
    monkeypatch.setattr(
        engine,
        "_cycle",
        AsyncMock(side_effect=(
            bot_engine_module._CycleResult(100.0, "NONE", "warm one", 0),
            bot_engine_module._CycleResult(101.0, "NONE", "warm two", 0),
        )),
    )
    monkeypatch.setattr(
        engine,
        "_cycle_with_retry",
        AsyncMock(side_effect=RuntimeError("loop failed")),
    )

    async def scenario():
        await engine.start(client, _engine_config(check_interval=0.001))
        first_task = engine._task
        await first_task
        assert engine.running is False
        assert engine.engine_state == "halted"
        assert engine.error_msg == "Paper strategy stopped unexpectedly"
        assert "loop failed" not in engine.error_msg
        await engine.start(client, _engine_config())
        assert engine.running is True
        assert engine._task is not first_task
        await engine.stop()

    asyncio.run(scenario())


def test_new_runtime_does_not_reuse_previous_runtime_action(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    _install_runtime(monkeypatch, object())
    engine.last_action = "[SAR+ADX paper] OPEN LONG 1 @ 100"
    monkeypatch.setattr(
        engine,
        "_cycle",
        AsyncMock(return_value=bot_engine_module._CycleResult(101.0, "NONE", "", 0)),
    )

    async def scenario():
        await engine.start(client, _engine_config())
        assert engine.last_action == ""
        await engine.stop()

    asyncio.run(scenario())


def test_task_creation_failure_rolls_back_start(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    _install_runtime(monkeypatch, object())
    monkeypatch.setattr(
        engine,
        "_cycle",
        AsyncMock(return_value=bot_engine_module._CycleResult(100.0, "NONE", "", 0)),
    )

    async def loop(*_args):
        return None

    def fail_create_task(coroutine, *_args, **_kwargs):
        coroutine.close()
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(engine, "_loop", loop)

    async def scenario():
        monkeypatch.setattr(asyncio, "create_task", fail_create_task)
        with pytest.raises(RuntimeError, match="task creation failed"):
            await engine.start(client, _engine_config())

    asyncio.run(scenario())
    _assert_start_rolled_back(engine)


def test_cancelled_stop_still_awaits_worker_terminal(monkeypatch):
    engine = BotEngine()
    client = _ReadOnlyClient()
    worker_cancelled = asyncio.Event()
    release_worker = asyncio.Event()
    _install_runtime(monkeypatch, object())
    monkeypatch.setattr(
        engine,
        "_cycle",
        AsyncMock(return_value=bot_engine_module._CycleResult(100.0, "NONE", "", 0)),
    )

    async def loop(*_args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            worker_cancelled.set()
            await release_worker.wait()
            raise

    monkeypatch.setattr(engine, "_loop", loop)

    async def scenario():
        await engine.start(client, _engine_config())
        worker = engine._task
        stop = asyncio.create_task(engine.stop())
        await worker_cancelled.wait()
        stop.cancel()
        stop.cancel()
        await asyncio.sleep(0)
        assert not stop.done()
        assert not worker.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert worker.done()
        assert engine.running is False
        assert engine._task is None

    asyncio.run(scenario())
