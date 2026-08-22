import asyncio
from decimal import Decimal
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.services import bot_engine as bot_engine_module
from backend.app.services.bot_engine import BotEngine
from backend.app.services.binance_errors import BinanceGatewayUnavailable
from backend.app.services.exchange_executor import (
    AccountValidation,
    ExchangePosition,
    ExecutionResult,
    OrderIntentType,
    RecoveryRequiredError,
    SymbolRules,
)
from backend.app.services.execution_store import ExecutionStore
from backend.app.services.live_strategy_runtime import (
    DecisionPlan,
    LiveStrategyRuntimeError,
)
from backend.app.strategies.sar_pyramid import (
    SarPyramidAction,
    SarPyramidActionType,
    SarPyramidState,
)
from backend.app.strategies.sar_layered import (
    SarLayeredAction,
    SarLayeredActionType,
    SarLayeredState,
)


def _engine_config(**overrides):
    values = {
        "name": "CandleMind Trend Strategy",
        "symbol": "SOLUSDT",
        "interval": "5m",
        "strategy_type": "sar_adx_pyramid",
        "config_version": "sar_adx_v3",
        "capital_limit": 250.0,
        "network": "testnet",
        "check_interval": 3600,
    }
    values.update(overrides)
    return values


def _position(direction=0, quantity="0"):
    return ExchangePosition(
        symbol="SOLUSDT",
        direction=direction,
        quantity=Decimal(quantity),
        entry_price=Decimal("100") if direction else Decimal("0"),
        isolated=True,
        leverage=1,
    )


def _snapshot():
    return bot_engine_module._MarketSnapshot(
        klines=[
            [
                0,
                "100",
                "101",
                "99",
                "100",
                "1",
                299999,
                "100",
                1,
                "0.5",
                "50",
                "0",
            ]
        ],
        server_time=300010,
        funding=[],
        exchange_info={"symbols": [{"symbol": "SOLUSDT", "status": "TRADING"}]},
        mark_price={"markPrice": "100"},
    )


def _plan(*actions, reason=None):
    return DecisionPlan(
        decision_id="bar-299999",
        actions=tuple(actions),
        proposed_state=SarPyramidState(),
        decision_time=pd.Timestamp("1970-01-01T00:04:59.999Z"),
        execution_time=pd.Timestamp("1970-01-01T00:05:00Z"),
        reference_price=100.0,
        no_action_reason=reason,
    )


def _store(tmp_path):
    store = ExecutionStore(tmp_path)
    store.initialize(
        "testnet",
        "SOLUSDT",
        run_id="cm-testnet-SOLUSDT-sar_adx_v3",
        metadata={"strategy_type": "sar_adx_pyramid", "config_version": "sar_adx_v3"},
    )
    return store


class _Runtime:
    def __init__(self, plan=None, error=None):
        self.plan = plan
        self.error = error
        self.prepare_calls = 0
        self.committed = []
        self.config = SimpleNamespace(
            layers=5,
            target_notional_fraction=1.0,
        )

    def prepare_decision(self, *_args, **_kwargs):
        self.prepare_calls += 1
        if self.error:
            raise self.error
        return self.plan

    def commit(self, plan):
        self.committed.append(plan)


class _Executor:
    def __init__(self, *, position=None, error=None):
        self.position = position or _position()
        self.error = error
        self.intents = []
        self.weighted_calls = []

    def current_position(self, _symbol):
        return self.position

    def available_balance(self):
        return Decimal("1000")

    def layer_quantity(self, **_kwargs):
        return Decimal("0.5")

    def weighted_layer_quantity(self, **kwargs):
        self.weighted_calls.append(kwargs)
        return Decimal("0.25")

    def execute(self, intent):
        self.intents.append(intent)
        if self.error:
            raise self.error
        if intent.action in {OrderIntentType.OPEN, OrderIntentType.ADD}:
            total = self.position.quantity + intent.quantity
            self.position = _position(intent.direction, str(total))
        else:
            self.position = _position()
        return ExecutionResult(
            symbol="SOLUSDT",
            action=intent.action,
            status="FILLED",
            side="BUY",
            quantity=intent.quantity,
            executed_quantity=intent.quantity,
            average_price=Decimal("100"),
            order_id=123,
            client_order_id="cm-order-123",
            recovered_after_ambiguous_submit=False,
            raw={},
        )


class _SnapshotClient:
    def __init__(self, failures=0, error_factory=None):
        self.failures = failures
        self.error_factory = error_factory or (
            lambda: RequestsConnectionError("proxy credentials and URL")
        )
        self.calls = []

    def futures_klines(self, **_kwargs):
        self.calls.append("klines")
        if self.failures:
            self.failures -= 1
            raise self.error_factory()
        return _snapshot().klines

    def futures_time(self):
        self.calls.append("server_time")
        return {"serverTime": _snapshot().server_time}

    def futures_funding_rate(self, **_kwargs):
        self.calls.append("funding_rate")
        return []

    def futures_exchange_info(self):
        self.calls.append("exchange_info")
        return _snapshot().exchange_info

    def futures_mark_price(self, **_kwargs):
        self.calls.append("mark_price")
        return _snapshot().mark_price


def test_baseline_decision_is_journaled_and_never_submits_order(tmp_path):
    engine = BotEngine()
    runtime = _Runtime(_plan(reason="baseline"))
    executor = _Executor()
    store = _store(tmp_path)

    result = engine._process_snapshot(
        _snapshot(), "SOLUSDT", runtime, executor, store, "testnet", 250.0
    )

    assert executor.intents == []
    assert runtime.committed == [runtime.plan]
    assert result.fill_count == 0
    assert result.no_action_reason == "baseline"
    assert store.status_summary("testnet", "SOLUSDT")["decision_count"] == 1
    assert store.status_summary("testnet", "SOLUSDT")["order_attempt_count"] == 0


def test_signal_executes_through_executor_and_updates_journal_counters(tmp_path):
    engine = BotEngine()
    runtime = _Runtime(_plan(SarPyramidAction(SarPyramidActionType.OPEN, 1)))
    executor = _Executor()
    store = _store(tmp_path)

    result = engine._process_snapshot(
        _snapshot(), "SOLUSDT", runtime, executor, store, "testnet", 250.0
    )
    summary = store.status_summary("testnet", "SOLUSDT")

    assert len(executor.intents) == 1
    assert executor.intents[0].action is OrderIntentType.OPEN
    assert executor.intents[0].quantity == Decimal("0.5")
    assert runtime.committed == [runtime.plan]
    assert result.last_action == "OPEN LONG"
    assert result.last_exchange_order_id == "123"
    assert summary["decision_count"] == 1
    assert summary["order_attempt_count"] == 1
    assert summary["submitted_order_count"] == 1
    assert summary["filled_order_count"] == 1
    assert summary["unknown_order_count"] == 0


def test_layered_signal_uses_the_action_capital_weight(tmp_path):
    engine = BotEngine()
    plan = DecisionPlan(
        decision_id="bar-layered",
        actions=(SarLayeredAction(SarLayeredActionType.OPEN, 1, 1, 0.2),),
        proposed_state=SarLayeredState(),
        decision_time=pd.Timestamp("1970-01-01T00:04:59.999Z"),
        execution_time=pd.Timestamp("1970-01-01T00:05:00Z"),
        reference_price=100.0,
        no_action_reason=None,
    )
    runtime = _Runtime(plan)
    runtime.serialize_state = lambda _state: {"last_processed_close_time": None}
    executor = _Executor()
    store = _store(tmp_path)

    engine._process_snapshot(
        _snapshot(), "SOLUSDT", runtime, executor, store, "testnet", 250.0
    )

    assert executor.weighted_calls[0]["capital_weight"] == 0.2
    assert executor.intents[0].quantity == Decimal("0.25")


def test_post_journal_analytics_failure_does_not_repeat_or_block_order(tmp_path):
    engine = BotEngine()
    runtime = _Runtime(_plan(SarPyramidAction(SarPyramidActionType.OPEN, 1)))
    executor = _Executor()
    store = _store(tmp_path)

    class FailingAnalytics:
        def capture_order(self, *_args, **_kwargs):
            raise RuntimeError("analytics unavailable")

    engine._analytics_service = FailingAnalytics()
    engine._analytics_scope_id = 1
    engine._analytics_run_id = "run"

    result = engine._process_snapshot(
        _snapshot(), "SOLUSDT", runtime, executor, store, "testnet", 250.0
    )

    assert result.last_exchange_order_id == "123"
    assert len(executor.intents) == 1
    assert store.status_summary("testnet", "SOLUSDT")["filled_order_count"] == 1


def test_ambiguous_order_is_recorded_unknown_without_blind_resubmit(tmp_path):
    engine = BotEngine()
    runtime = _Runtime(_plan(SarPyramidAction(SarPyramidActionType.OPEN, 1)))
    executor = _Executor(error=RecoveryRequiredError("unknown exchange state"))
    store = _store(tmp_path)

    with pytest.raises(RecoveryRequiredError, match="unknown exchange state"):
        engine._process_snapshot(
            _snapshot(), "SOLUSDT", runtime, executor, store, "testnet", 250.0
        )

    assert len(executor.intents) == 1
    assert runtime.committed == []
    summary = store.status_summary("testnet", "SOLUSDT")
    assert summary["order_attempt_count"] == 1
    assert summary["unknown_order_count"] == 1


def test_stop_uses_exchange_executor_reduce_only_close(tmp_path):
    class Client:
        def __init__(self):
            self.created = []

        def futures_position_information(self, **_kwargs):
            return [
                {
                    "symbol": "SOLUSDT",
                    "positionAmt": "0.5",
                    "entryPrice": "100",
                    "isolated": True,
                    "leverage": "1",
                }
            ]

        def futures_create_order(self, **kwargs):
            self.created.append(kwargs)
            return {
                "symbol": "SOLUSDT",
                "orderId": 321,
                "clientOrderId": kwargs["newClientOrderId"],
                "status": "FILLED",
                "side": kwargs["side"],
                "executedQty": kwargs["quantity"],
                "avgPrice": "100",
            }

    client = Client()
    rules = SymbolRules(Decimal("0.1"), Decimal("0.1"), Decimal("100"), Decimal("5"))
    engine = BotEngine()
    engine._executor = bot_engine_module.ExchangeExecutor(client, rules)
    engine._execution_store = _store(tmp_path)
    engine._network = "testnet"
    engine._symbol = "SOLUSDT"

    asyncio.run(engine._close_exchange_position("strategy_stop"))

    assert len(client.created) == 1
    assert client.created[0]["side"] == "SELL"
    assert client.created[0]["reduceOnly"] is True
    assert client.created[0]["positionSide"] == "BOTH"
    assert engine._execution_store.status_summary("testnet", "SOLUSDT")[
        "filled_order_count"
    ] == 1


def test_stop_waits_for_inflight_decision_thread_before_closing(tmp_path):
    engine = BotEngine()
    executor = _Executor()
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    events = []

    def decision():
        entered.set()
        release.wait(timeout=2)
        executor.position = _position(1, "0.5")
        events.append("decision_finished")

    original_execute = executor.execute

    def execute(intent):
        events.append("close_submitted")
        return original_execute(intent)

    executor.execute = execute
    engine._executor = executor
    engine._execution_store = store
    engine._network = "testnet"
    engine._symbol = "SOLUSDT"
    engine.running = True

    async def scenario():
        worker = asyncio.create_task(engine._run_execution_critical(decision))
        engine._task = worker
        while not entered.is_set():
            await asyncio.sleep(0)

        stopping = asyncio.create_task(engine.stop())
        await asyncio.sleep(0.05)
        assert stopping.done() is False
        assert events == []

        release.set()
        await stopping

    asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))

    assert events == ["decision_finished", "close_submitted"]
    assert executor.position.direction == 0
    assert engine._task is None


def test_status_exposes_exchange_execution_fields(tmp_path):
    engine = BotEngine()
    store = _store(tmp_path)
    store.record_decision(
        "testnet", "SOLUSDT", decision_id="decision-1", action="OPEN"
    )
    store.record_order_attempt(
        "testnet",
        "SOLUSDT",
        decision_id="decision-1",
        ordinal=0,
        request={"action": "open", "direction": 1, "quantity": "0.5"},
    )
    store.record_order_result(
        "testnet",
        "SOLUSDT",
        decision_id="decision-1",
        ordinal=0,
        status="filled",
        exchange_order_id=123,
        filled_quantity="0.5",
    )
    engine._execution_store = store
    engine._network = "testnet"
    engine._symbol = "SOLUSDT"
    engine._strategy_name = "CandleMind Trend Strategy"
    engine._strategy_type = "sar_adx_pyramid"
    engine._config_version = "sar_adx_v3"
    engine._capital_limit = 250.0
    engine.no_action_reason = "no_strategy_action"
    engine.last_exchange_order_id = "123"

    status = engine.status

    assert status["paper"] is False
    assert status["network"] == "testnet"
    assert status["capital_limit"] == 250.0
    assert status["decision_count"] == 1
    assert status["order_attempt_count"] == 1
    assert status["submitted_order_count"] == 1
    assert status["filled_order_count"] == 1
    assert status["rejected_order_count"] == 0
    assert status["unknown_order_count"] == 0
    assert status["no_action_reason"] == "no_strategy_action"
    assert status["last_exchange_order_id"] == "123"


def test_transient_snapshot_failure_retries_full_snapshot_and_recovers(
    monkeypatch, tmp_path
):
    engine = BotEngine()
    engine.running = True
    client = _SnapshotClient(failures=1)
    runtime = _Runtime(None)
    executor = _Executor()
    store = _store(tmp_path)
    observed = []

    def immediate_sleep(_delay):
        observed.append((engine.engine_state, engine.failure_count, engine.next_retry_at))

    monkeypatch.setattr(bot_engine_module.time, "sleep", immediate_sleep)
    result = asyncio.run(
        engine._cycle_with_retry(
            client, "SOLUSDT", runtime, executor, store, "testnet", 250.0
        )
    )

    assert result.no_action_reason == "bar_already_processed"
    assert client.calls.count("klines") == 2
    assert client.calls.count("server_time") == 1
    assert client.calls.count("funding_rate") == 1
    assert client.calls.count("exchange_info") == 1
    assert client.calls.count("mark_price") == 1
    assert runtime.prepare_calls == 1
    assert observed[0][0:2] == ("retrying", 1)
    assert observed[0][2] is not None
    assert engine.engine_state == "running"
    assert engine.failure_count == 0
    assert engine.error_code is None
    assert engine.last_success_at is not None


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (RequestsTimeout, "Binance request timed out"),
        (RequestsConnectionError, "Binance is temporarily unavailable"),
    ],
)
def test_retry_exhaustion_halts_with_sanitized_network_state(
    monkeypatch, tmp_path, error_type, message
):
    engine = BotEngine()
    client = _SnapshotClient(
        failures=10,
        error_factory=lambda: error_type("secret proxy detail"),
    )
    runtime = _Runtime(None)
    executor = _Executor()
    store = _store(tmp_path)
    real_sleep = asyncio.sleep

    async def immediate_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(bot_engine_module.asyncio, "sleep", immediate_sleep)

    async def scenario():
        engine.running = True
        task = asyncio.create_task(
            engine._loop(
                client,
                "SOLUSDT",
                runtime,
                executor,
                store,
                "testnet",
                250.0,
                0,
            )
        )
        engine._task = task
        await task

    asyncio.run(scenario())

    assert client.calls.count("klines") == 3
    assert runtime.prepare_calls == 0
    assert engine.running is False
    assert engine.engine_state == "network_halted"
    assert engine.failure_count == 3
    assert engine.error_code == "upstream_unavailable"
    assert engine.error_msg == message
    assert "secret" not in engine.error_msg


def test_bot_engine_uses_gateway_retry_contract_without_local_retry_helpers():
    assert not hasattr(BotEngine, "_retry_details")
    assert not hasattr(BotEngine, "_retry_delay")

    engine = BotEngine()
    error = BinanceGatewayUnavailable("Binance is temporarily unavailable")
    engine._set_terminal_error(error)
    assert engine.engine_state == "halted"


def test_runtime_failure_requires_reconciliation():
    engine = BotEngine()

    engine._set_terminal_error(LiveStrategyRuntimeError("private state detail"))

    assert engine.engine_state == "recovery_required"
    assert engine.error_code == "runtime_recovery_required"
    assert engine.error_msg == "Strategy execution requires reconciliation"
    assert "private" not in engine.error_msg


def test_no_action_cycle_does_not_replace_last_real_action():
    engine = BotEngine()
    engine.last_action = "OPEN LONG"

    engine._apply_cycle(
        bot_engine_module._CycleResult(
            102.0, "LONG", "", 0, "no_strategy_action", None
        )
    )

    assert engine.last_action == "OPEN LONG"
    assert engine.last_signal == "LONG"
    assert engine.no_action_reason == "no_strategy_action"


class _StartExecutor(_Executor):
    def validate_one_way_account(self, _symbol, **_kwargs):
        return AccountValidation("SOLUSDT", Decimal("0"), 0)

    def validate_symbol_risk(self, _symbol):
        return _position()


class _StartClient:
    def futures_exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "SOLUSDT",
                    "status": "TRADING",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.1",
                            "minQty": "0.1",
                            "maxQty": "100",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }


def _install_start_dependencies(monkeypatch, tmp_path, runtime=None, executor=None):
    runtime = runtime or _Runtime(None)
    executor = executor or _StartExecutor()
    store = _store(tmp_path)
    monkeypatch.setattr(bot_engine_module, "ExchangeExecutor", lambda *_args: executor)
    monkeypatch.setattr(bot_engine_module, "ExecutionStore", lambda: store)
    monkeypatch.setattr(bot_engine_module, "LiveStrategyRuntime", lambda *_args, **_kwargs: runtime)
    return runtime, executor, store


def test_concurrent_identical_starts_commit_only_one_runtime(monkeypatch, tmp_path):
    engine = BotEngine()
    runtime, _executor, _store_instance = _install_start_dependencies(
        monkeypatch, tmp_path
    )
    warmup_entered = asyncio.Event()
    release_warmup = asyncio.Event()
    warmup_count = 0

    async def cycle(*_args, **_kwargs):
        nonlocal warmup_count
        warmup_count += 1
        warmup_entered.set()
        await release_warmup.wait()
        return bot_engine_module._CycleResult(100.0, "NONE", "", 0, "baseline")

    async def loop(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "_cycle", cycle)
    monkeypatch.setattr(engine, "_loop", loop)

    async def scenario():
        first = asyncio.create_task(engine.start(_StartClient(), _engine_config()))
        await warmup_entered.wait()
        second = asyncio.create_task(engine.start(_StartClient(), _engine_config()))
        await asyncio.sleep(0)
        release_warmup.set()
        await asyncio.gather(first, second)
        assert engine.running is True
        assert engine._sar_adx_runtime is runtime
        assert warmup_count == 1
        worker = engine._task
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        engine._clear_runtime()

    asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))


def test_warmup_failure_rolls_back_staged_runtime(monkeypatch, tmp_path):
    engine = BotEngine()
    _install_start_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        engine, "_cycle", AsyncMock(side_effect=RuntimeError("warmup failed"))
    )

    with pytest.raises(RuntimeError, match="warmup failed"):
        asyncio.run(engine.start(_StartClient(), _engine_config()))

    assert engine.running is False
    assert engine._task is None
    assert engine._sar_adx_runtime is None
    assert engine._executor is None
    assert engine._symbol == ""


def test_cancelled_start_waits_for_warmup_order_then_flattens(
    monkeypatch, tmp_path
):
    engine = BotEngine()
    _runtime, executor, _store_instance = _install_start_dependencies(
        monkeypatch, tmp_path
    )
    entered = threading.Event()
    release = threading.Event()
    events = []

    async def fetch_snapshot(*_args, **_kwargs):
        return _snapshot()

    def process_snapshot(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        executor.position = _position(1, "0.5")
        events.append("warmup_finished")
        return bot_engine_module._CycleResult(100.0, "LONG", "OPEN LONG", 1)

    original_execute = executor.execute

    def execute(intent):
        events.append("cleanup_close")
        return original_execute(intent)

    executor.execute = execute
    monkeypatch.setattr(engine, "_fetch_snapshot", fetch_snapshot)
    monkeypatch.setattr(engine, "_process_snapshot", process_snapshot)

    async def scenario():
        starting = asyncio.create_task(
            engine.start(_StartClient(), _engine_config())
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        starting.cancel()
        await asyncio.sleep(0.05)
        assert starting.done() is False
        assert events == []

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting

    asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))

    assert events == ["warmup_finished", "cleanup_close"]
    assert executor.position.direction == 0
    assert engine._task is None
    assert engine._executor is None


@pytest.mark.parametrize(
    "override",
    [
        {"strategy_type": "ml_trend"},
        {"config_version": "sar_adx_v2"},
        {"network": "paper"},
        {"interval": "15m"},
        {"capital_limit": float("nan")},
    ],
)
def test_invalid_live_configuration_is_rejected_before_exchange_access(override):
    engine = BotEngine()

    class Client:
        def __getattr__(self, name):
            raise AssertionError(f"invalid start accessed Binance client: {name}")

    with pytest.raises(ValueError):
        asyncio.run(engine.start(Client(), _engine_config(**override)))
