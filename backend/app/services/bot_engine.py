"""Transactional coordinator for exchange-backed trend strategy execution."""

from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pandas as pd
from loguru import logger

from backend.app.strategies.sar_pyramid import (
    PositionSnapshot,
    SarPyramidActionType,
    SarPyramidConfig,
)
from backend.app.exchanges.binance.adapter import build_binance_adapter
from backend.app.exchanges.contracts import (
    ExchangeBinding,
    ExecutionAuthorization,
    OrderAction,
    OrderRequest,
)

from .exchange_executor import (
    ExchangeExecutionError,
    ExchangeExecutor,
    OrderIntent,
    OrderIntentType,
    RecoveryRequiredError,
    SymbolRules,
    UnsupportedAccountError,
)
from .execution_store import ExecutionStore, ExecutionStoreError
from .binance_errors import BinanceGatewayError
from .binance_retry import BinanceRetryExecutor, BinanceRetryPolicy
from .binance_usdm_gateway import BinanceUsdMGateway
from .strategy_analytics import StrategyAnalyticsService, account_fingerprint
from .live_strategy_runtime import (
    DecisionPlan,
    LiveStrategyRuntime,
    LiveStrategyRuntimeError,
)
from .live_layered_runtime import LiveLayeredStrategyRuntime
from .strategy_configuration import configuration_hash
from .strategy_runtime_intent import (
    StrategyRuntimeIntentStore,
    StrategyScope,
    TradingLease,
)


STRATEGY_TYPE = "sar_adx_pyramid"
CONFIG_VERSION = "sar_adx_v3"
BAR_INTERVAL = "5m"
SUPPORTED_STRATEGY_VERSIONS = {
    STRATEGY_TYPE: CONFIG_VERSION,
    "sar_adx_trend": "sar_adx_trend_v1",
    "sar_martingale": "sar_martingale_v1",
    "sar_anti_martingale": "sar_anti_martingale_v1",
}


@dataclass(frozen=True)
class _CycleResult:
    mark_price: float
    last_signal: str
    last_action: str
    fill_count: int
    no_action_reason: str | None = None
    last_exchange_order_id: str | None = None


@dataclass(frozen=True)
class _MarketSnapshot:
    klines: list
    server_time: int
    funding: list
    exchange_info: dict
    mark_price: dict


@dataclass(frozen=True)
class _JournalPosition:
    direction: int = 0
    quantity: Decimal = Decimal("0")
    layers: int = 0
    anchor: float | None = None


class _SnapshotFetchError(Exception):
    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


class BotEngine:
    """Own one exchange-backed decision runtime and its polling task."""

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self.last_signal = "NONE"
        self.last_action = ""
        self.error_msg = ""
        self.engine_state = "stopped"
        self.failure_count = 0
        self.last_success_at: str | None = None
        self.next_retry_at: str | None = None
        self.error_code: str | None = None
        self._strategy_name = ""
        self._strategy_type = ""
        self._config_version = ""
        self._config_hash = ""
        self._symbol = ""
        self.paper = False
        self._capital_limit = 0.0
        self._network = ""
        self.circuit_open = False
        self._sar_adx_runtime: LiveStrategyRuntime | None = None
        self._executor: ExchangeExecutor | None = None
        self._execution_store: ExecutionStore | None = None
        self._client = None
        self._sar_adx_mark_price: float | None = None
        self._check_interval: float | None = None
        self._cached_symbol = ""
        self._cached_direction: str | None = None
        self._cached_fill_count: int | None = None
        self._cached_fill_count_complete = False
        self.no_action_reason: str | None = None
        self.last_exchange_order_id: str | None = None
        self._analytics_service: StrategyAnalyticsService | None = None
        self._analytics_scope_id: int | None = None
        self._analytics_run_id: str | None = None
        self._intent_store: StrategyRuntimeIntentStore | None = None
        self._trading_lease: TradingLease | None = None
        self._runtime_scope: StrategyScope | None = None
        self._runtime_config: dict[str, Any] | None = None

    @property
    def status(self) -> dict:
        summary = self._execution_summary()
        position_direction = self._cached_direction or "NONE"
        status = {
            "running": self.running,
            "engine_state": self.engine_state,
            "last_signal": position_direction or "NONE",
            "position_direction": position_direction,
            "last_action": self.last_action,
            "trade_count": summary["filled_order_count"],
            "error": self.error_msg,
            "error_code": self.error_code,
            "failure_count": self.failure_count,
            "last_success_at": self.last_success_at,
            "next_retry_at": self.next_retry_at,
            "strategy_name": self._strategy_name,
            "strategy_type": self._strategy_type,
            "config_version": self._config_version,
            "config_hash": self._config_hash or None,
            "symbol": self._symbol,
            "paper": False,
            "network": self._network or None,
            "capital_limit": self._capital_limit or None,
            "circuit_open": self.circuit_open,
            "no_action_reason": self.no_action_reason,
            "last_exchange_order_id": self.last_exchange_order_id,
            **summary,
        }
        return status

    async def assert_configuration_change_safe(
        self, client, symbol: str, network: str
    ) -> None:
        """Fail closed unless execution is fully stopped, reconciled, and flat."""

        async with self._lifecycle_lock:
            await self._reap_terminal_task()
            if self.running or self._task is not None or self.engine_state != "stopped":
                raise ValueError("strategy execution is not fully stopped")
            normalized = symbol.strip().upper()
            gateway = BinanceUsdMGateway(client)
            exchange_info = await asyncio.to_thread(gateway.exchange_info)
            executor = ExchangeExecutor(
                client,
                SymbolRules.from_exchange_info(exchange_info, normalized),
                network,
            )
            store = ExecutionStore()
            await asyncio.to_thread(
                self._reconcile_execution_journal,
                executor,
                store,
                network,
                normalized,
            )
            journal_position = self._journal_position(store, network, normalized)
            validation = await asyncio.to_thread(
                executor.validate_one_way_account,
                normalized,
                allow_existing_position=journal_position.direction != 0,
            )
            position = await asyncio.to_thread(
                executor.validate_symbol_risk, normalized
            )
            if validation.open_order_count:
                raise ValueError("open orders prevent changing strategy configuration")
            if position.direction or journal_position.direction:
                raise ValueError("an open position prevents changing strategy configuration")

    def has_execution_journal(self, symbol: str, network: str) -> bool:
        return ExecutionStore().load(network, symbol.strip().upper()) is not None

    def _execution_summary(self) -> dict[str, int]:
        empty = {
            "decision_count": 0,
            "order_attempt_count": 0,
            "submitted_order_count": 0,
            "filled_order_count": 0,
            "rejected_order_count": 0,
            "unknown_order_count": 0,
        }
        if not self._execution_store or not self._network or not self._symbol:
            return empty
        summary = self._execution_store.status_summary(self._network, self._symbol)
        if summary is None:
            return empty
        return {key: int(summary.get(key, 0)) for key in empty}

    async def start(self, client, cfg: dict) -> None:
        """Start only after all fallible initialization has completed."""

        symbol, capital_limit, check_interval, network = self._validate_config(cfg)
        async with self._lifecycle_lock:
            await self._reap_terminal_task()
            if self.running:
                if self._same_runtime(cfg):
                    return
                raise ValueError(f"strategy already running for {self._symbol}")
            if self._task is not None:
                if self._same_runtime(cfg):
                    return
                raise ValueError(f"strategy already running for {self._symbol}")

            gateway = BinanceUsdMGateway(client)
            exchange_info = await asyncio.to_thread(gateway.exchange_info)
            if not self._is_tradable(exchange_info, symbol):
                raise ValueError(f"symbol is not currently tradable: {symbol}")

            executor = ExchangeExecutor(
                client,
                SymbolRules.from_exchange_info(exchange_info, symbol),
                network,
            )
            store = ExecutionStore()
            strategy_type = str(cfg["strategy_type"])
            config_version = str(cfg["config_version"])
            config_hash = str(cfg.get("config_hash") or "legacy")
            capital_identity = format(capital_limit, ".12g")
            run_id = (
                f"cm-{network}-{symbol}-{config_version}-{config_hash[:12]}-"
                f"cap{capital_identity}"
            )
            metadata = {
                "strategy_type": strategy_type,
                "config_version": config_version,
                "config_hash": config_hash,
                "capital_limit": capital_identity,
            }
            existing = store.load(network, symbol)
            if existing is not None and {
                "run_id": existing["run"]["run_id"],
                "metadata": existing["run"]["metadata"],
            } != {"run_id": run_id, "metadata": metadata}:
                await asyncio.to_thread(
                    self._reconcile_execution_journal,
                    executor,
                    store,
                    network,
                    symbol,
                )
                previous_position = self._journal_position(store, network, symbol)
                previous_validation = await asyncio.to_thread(
                    executor.validate_one_way_account,
                    symbol,
                    allow_existing_position=previous_position.direction != 0,
                )
                exchange_position = await asyncio.to_thread(
                    executor.validate_symbol_risk, symbol
                )
                if previous_validation.open_order_count:
                    raise UnsupportedAccountError(
                        "existing open orders prevent a strategy configuration switch"
                    )
                if (
                    exchange_position.direction != previous_position.direction
                    or exchange_position.quantity != previous_position.quantity
                ):
                    raise UnsupportedAccountError(
                        "exchange position does not match the previous execution journal"
                    )
                if previous_position.direction:
                    raise UnsupportedAccountError(
                        "close the existing position before switching strategy configuration"
                    )
                await asyncio.to_thread(store.archive, network, symbol)
            store.initialize(
                network,
                symbol,
                run_id=run_id,
                metadata=metadata,
            )
            await asyncio.to_thread(
                self._reconcile_execution_journal,
                executor,
                store,
                network,
                symbol,
            )
            restored = self._restored_strategy_state(store, network, symbol)
            runtime = self._build_runtime(symbol, cfg, restored)
            journal_position = self._journal_position(store, network, symbol)
            validation = await asyncio.to_thread(
                executor.validate_one_way_account,
                symbol,
                allow_existing_position=journal_position.direction != 0,
            )
            position = await asyncio.to_thread(executor.validate_symbol_risk, symbol)
            if position.direction != journal_position.direction:
                raise UnsupportedAccountError("exchange position does not match the execution journal")
            if position.direction and position.quantity != Decimal(str(journal_position.quantity)):
                raise UnsupportedAccountError("exchange position quantity does not match the execution journal")
            if validation.open_order_count:
                raise UnsupportedAccountError("existing open orders require reconciliation")
            available_balance = await asyncio.to_thread(executor.available_balance)
            if available_balance <= 0:
                raise UnsupportedAccountError("available USDT balance is zero")
            if isinstance(runtime, LiveLayeredStrategyRuntime):
                mark_payload = await asyncio.to_thread(
                    gateway.mark_price, symbol=symbol
                )
                reference_price = float(mark_payload["markPrice"])
                for weight in runtime.config.layer_weights:
                    executor.weighted_layer_quantity(
                        available_balance=available_balance,
                        capital_limit=capital_limit,
                        reference_price=reference_price,
                        capital_weight=weight,
                    )
            self._capture_analytics_run(
                client, store, network, symbol, run_id, capital_limit,
                strategy_type=strategy_type,
                config_version=config_version,
                resume_existing=journal_position.direction != 0,
            )
            try:
                first_cycle = await self._cycle(
                    client,
                    symbol,
                    runtime,
                    executor=executor,
                    store=store,
                    network=network,
                    capital_limit=capital_limit,
                )
            except asyncio.CancelledError:
                try:
                    await self._close_position_context(
                        executor,
                        store,
                        network,
                        symbol,
                        "strategy_start_cancelled",
                    )
                finally:
                    self._clear_runtime()
                raise

            task: asyncio.Task[None] | None = None
            try:
                task = asyncio.create_task(
                    self._loop(
                        client, symbol, runtime, executor, store, network,
                        capital_limit, check_interval,
                    ),
                    name=f"candlemind-live:{network}:{symbol}",
                )
                self.error_msg = ""
                self._strategy_name = cfg.get("name", "CandleMind Trend Strategy")
                self._strategy_type = strategy_type
                self._config_version = config_version
                self._config_hash = config_hash
                self._symbol = symbol
                self.paper = False
                self._capital_limit = capital_limit
                self._network = network
                self.circuit_open = False
                self._sar_adx_runtime = runtime
                self._executor = executor
                self._execution_store = store
                self._client = client
                self._check_interval = check_interval
                self._cached_symbol = symbol
                self.last_signal = "NONE"
                self.last_action = ""
                if first_cycle is not None:
                    self._apply_cycle(first_cycle)
                self.running = True
                self.engine_state = "running"
                self._clear_failure_state()
                self._task = task
                self._intent_store = cfg.get("_intent_store")
                self._trading_lease = cfg.get("_trading_lease")
                self._runtime_scope = cfg.get("_runtime_scope")
                self._runtime_config = {
                    key: value for key, value in cfg.items() if not key.startswith("_")
                }
            except BaseException:
                if task is not None:
                    task.cancel()
                    await self._await_terminal(task)
                self._clear_runtime()
                raise

            logger.info(
                "Engine started: {} {} [{}] network={}",
                symbol,
                BAR_INTERVAL,
                strategy_type,
                network,
            )

    async def stop(self) -> None:
        """Stop by user request and flatten the managed exchange position."""

        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                self.running = False
                self.engine_state = "stopped"
                return

            self.running = False
            self.engine_state = "stopped"
            if not task.done():
                task.cancel()
            cancellation: asyncio.CancelledError | None = None
            stopped_cleanly = False
            try:
                try:
                    await self._await_terminal(task)
                except asyncio.CancelledError as exc:
                    cancellation = exc
                await self._close_exchange_position("strategy_stop")
                self._record_stopped_intent()
                stopped_cleanly = True
            finally:
                if stopped_cleanly:
                    self._release_trading_lease()
                else:
                    self.engine_state = "recovery_required"
                if stopped_cleanly and self._task is task and task.done():
                    self._clear_runtime()
            if cancellation is not None:
                raise cancellation
            logger.info("Engine stopped")

    async def stop_persisted(self, client: Any, network: str, symbol: str) -> None:
        """Reconcile and flatten an audited persisted runtime after restart."""

        normalized = symbol.strip().upper()
        async with self._lifecycle_lock:
            if self._task is not None or self.running:
                raise ValueError("an in-process strategy runtime is already active")
            gateway = BinanceUsdMGateway(client)
            exchange_info = await asyncio.to_thread(gateway.exchange_info)
            executor = ExchangeExecutor(
                client,
                SymbolRules.from_exchange_info(exchange_info, normalized),
                network,
            )
            store = ExecutionStore()
            await asyncio.to_thread(
                self._reconcile_execution_journal,
                executor,
                store,
                network,
                normalized,
            )
            await self._close_position_context(
                executor, store, network, normalized, "strategy_stop"
            )
            self.running = False
            self.engine_state = "stopped"
            self.error_code = None
            self.error_msg = ""

    def apply_restart_audit(self, audit: dict[str, Any]) -> None:
        """Expose persisted operator intent without resuming execution."""

        if self._task is not None or self.running:
            return
        intent = audit.get("intent")
        if not isinstance(intent, dict) or intent.get("desired_state") != "running":
            return
        scope = intent.get("scope", {})
        config = intent.get("config", {})
        self._symbol = str(scope.get("symbol", ""))
        self._network = str(scope.get("network", ""))
        self._strategy_name = str(config.get("name", "CandleMind Trend Strategy"))
        self._strategy_type = str(config.get("strategy_type", ""))
        self._config_version = str(config.get("config_version", ""))
        self._config_hash = str(config.get("config_hash", ""))
        self._capital_limit = float(config.get("capital_limit", 0) or 0)
        self.engine_state = "recovery_required"
        self.error_code = "restart_audit_required"
        self.error_msg = "Persisted strategy requires reconciliation before resume"

    async def shutdown(self) -> None:
        """Drain the process runtime without creating exchange orders."""

        async with self._lifecycle_lock:
            task = self._task
            self.running = False
            recovery_required = self.engine_state == "recovery_required"
            if task is None:
                if not recovery_required:
                    self.engine_state = "stopped"
                return
            if not recovery_required:
                self.engine_state = "draining"
            if not task.done():
                task.cancel()
            try:
                await self._await_terminal(task)
            except asyncio.CancelledError:
                pass
            finally:
                recovery_required = (
                    recovery_required or self.engine_state == "recovery_required"
                )
                if not recovery_required:
                    self._release_trading_lease()
                if not recovery_required and self._task is task and task.done():
                    self._clear_runtime()
            logger.info("Engine runtime drained without flattening the exchange position")

    async def _close_exchange_position(self, reason: str) -> None:
        if not self._executor or not self._execution_store or not self._symbol:
            return
        await self._close_position_context(
            self._executor,
            self._execution_store,
            self._network,
            self._symbol,
            reason,
        )

    async def _close_position_context(
        self,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        symbol: str,
        reason: str,
    ) -> None:
        await self._run_execution_critical(
            self._close_position_sync,
            executor,
            store,
            network,
            symbol,
            reason,
        )

    def _close_position_sync(
        self,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        symbol: str,
        reason: str,
    ) -> None:
        position = executor.current_position(symbol)
        decision_id = f"stop:{uuid4().hex}"
        if not position.direction:
            store.record_decision(
                network,
                symbol,
                decision_id=decision_id,
                action="CLOSE",
                details={"reason": reason},
                expected_order_count=0,
            )
            return
        store.record_decision(
            network,
            symbol,
            decision_id=decision_id,
            action="CLOSE",
            details={"reason": reason},
            expected_order_count=1,
        )
        intent = OrderIntent(
            symbol=symbol,
            action=OrderIntentType.CLOSE,
            direction=position.direction,
            quantity=position.quantity,
            decision_id=decision_id,
        )
        self._record_attempt(intent, store=store, network=network)
        result = self._execute_intent(executor, intent, network)
        self._record_result(
            decision_id,
            0,
            result,
            store=store,
            network=network,
        )
        if result.status != "FILLED":
            store.transition_decision(
                network,
                symbol,
                decision_id=decision_id,
                status="recovery_required",
                error="stop order was not confirmed filled",
            )
            raise RecoveryRequiredError("stop order was not confirmed filled")
        store.transition_decision(
            network, symbol, decision_id=decision_id, status="committed"
        )

    async def _loop(
        self,
        client,
        symbol: str,
        runtime: Any,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        capital_limit: float,
        check_interval: float,
    ) -> None:
        task = asyncio.current_task()
        exhausted_snapshot_cycles = 0
        try:
            while True:
                await asyncio.sleep(check_interval)
                self._renew_trading_lease()
                try:
                    result = await self._cycle_with_retry(
                        client, symbol, runtime, executor, store, network, capital_limit
                    )
                except _SnapshotFetchError as exc:
                    cause = exc.cause
                    if not (
                        isinstance(cause, BinanceGatewayError)
                        and cause.failure
                        and cause.failure.retryable
                    ):
                        raise
                    exhausted_snapshot_cycles += 1
                    if exhausted_snapshot_cycles >= 3:
                        raise
                    self.engine_state = "retrying"
                    self.next_retry_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=max(check_interval, 1.0))
                    ).isoformat()
                    continue
                exhausted_snapshot_cycles = 0
                if self._task is not task:
                    return
                if result is not None:
                    self._apply_cycle(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._task is task:
                self._set_terminal_error(exc)
            self._log_terminal_error(symbol, exc)
        finally:
            if self._task is task:
                self.running = False

    async def _cycle(
        self,
        client,
        symbol: str,
        runtime: Any | None = None,
        *,
        executor: ExchangeExecutor | None = None,
        store: ExecutionStore | None = None,
        network: str | None = None,
        capital_limit: float | None = None,
    ) -> _CycleResult:
        """Advance the decision runtime from completed Binance 5m bars."""

        runtime = runtime or self._sar_adx_runtime
        if runtime is None:
            raise RuntimeError("strategy runtime is not initialized")
        executor = executor or self._executor
        store = store or self._execution_store
        network = network or self._network
        capital_limit = capital_limit or self._capital_limit
        if executor is None or store is None or not network or not capital_limit:
            raise RuntimeError("exchange execution is not initialized")

        try:
            snapshot = await self._fetch_snapshot(client, symbol)
        except _SnapshotFetchError as exc:
            raise exc.cause from exc
        return await self._run_execution_critical(
            self._process_snapshot, snapshot, symbol, runtime, executor, store,
            network, capital_limit,
        )

    async def _cycle_with_retry(
        self,
        client,
        symbol: str,
        runtime: Any,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        capital_limit: float,
    ) -> _CycleResult:
        try:
            snapshot = await self._fetch_snapshot(client, symbol)
        except _SnapshotFetchError as exc:
            self._observe_gateway_failure(exc.cause)
            self._log_snapshot_failure(symbol, exc)
            raise
        result = await self._run_execution_critical(
            self._process_snapshot, snapshot, symbol, runtime, executor, store,
            network, capital_limit,
        )
        self.engine_state = "running"
        self._clear_failure_state()
        return result

    async def _run_execution_critical(self, operation: Any, *args: Any) -> Any:
        """Run exchange decision work to completion even when its caller is cancelled."""

        async with self._execution_lock:
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await self._await_terminal(worker)
                except Exception as exc:
                    self._set_terminal_error(exc)
                    logger.error(
                        "Cancelled exchange operation finished with error: type={}",
                        type(exc).__name__,
                    )
                raise

    async def _fetch_snapshot(self, client, symbol: str) -> _MarketSnapshot:
        gateway = BinanceUsdMGateway(
            client,
            retry_executor=BinanceRetryExecutor(sleeper=self._observe_retry_delay),
        )
        batch = asyncio.gather(
            self._fetch_stage(
                "klines",
                gateway.klines,
                symbol=symbol,
                interval=BAR_INTERVAL,
                limit=500,
            ),
            self._fetch_stage("server_time", gateway.server_time),
            self._fetch_stage(
                "funding_rate",
                gateway.funding_rate,
                symbol=symbol,
                limit=100,
            ),
            self._fetch_stage("exchange_info", gateway.exchange_info),
            self._fetch_stage(
                "mark_price", gateway.mark_price, symbol=symbol
            ),
            return_exceptions=True,
        )
        try:
            results = await asyncio.shield(batch)
        except asyncio.CancelledError:
            while not batch.done():
                try:
                    await asyncio.shield(batch)
                except asyncio.CancelledError:
                    continue
            raise
        failures = [result for result in results if isinstance(result, BaseException)]
        for failure in failures:
            if (
                isinstance(failure, _SnapshotFetchError)
                and not isinstance(failure.cause, BinanceGatewayError)
            ):
                raise failure
        if failures:
            raise failures[0]
        raw, server, funding_raw, exchange_info, mark = results
        return _MarketSnapshot(raw, server, funding_raw, exchange_info, mark)

    @staticmethod
    async def _fetch_stage(stage: str, request: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(request, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _SnapshotFetchError(stage, exc) from exc

    def _process_snapshot(
        self,
        snapshot: _MarketSnapshot,
        symbol: str,
        runtime: Any,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        capital_limit: float,
    ) -> _CycleResult:
        raw = snapshot.klines
        server = snapshot.server_time
        exchange_info = snapshot.exchange_info
        mark = snapshot.mark_price

        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ]
        bars = pd.DataFrame(raw, columns=columns)
        if bars.empty:
            raise RuntimeError("Binance returned no 5m bars")

        exchange_position = executor.current_position(symbol)
        journal_position = self._journal_position(store, network, symbol)
        if exchange_position.direction != journal_position.direction:
            raise UnsupportedAccountError("exchange position diverged from the execution journal")
        if exchange_position.quantity != journal_position.quantity:
            raise UnsupportedAccountError("exchange quantity diverged from the execution journal")
        position = PositionSnapshot(
            direction=journal_position.direction,
            layers=journal_position.layers,
            anchor=journal_position.anchor,
        )
        plan = runtime.prepare_decision(
            bars,
            position,
            server_time=pd.Timestamp(server, unit="ms", tz="UTC"),
            execution_price=float(mark["markPrice"]),
            eligible=self._is_tradable(exchange_info, symbol),
        )
        mark_price = float(bars.iloc[-1]["close"])
        if plan is None:
            direction = {1: "LONG", -1: "SHORT"}.get(exchange_position.direction, "NONE")
            return _CycleResult(mark_price, direction, "", 0, "bar_already_processed")

        proposed_state = (
            runtime.serialize_state(plan.proposed_state)
            if hasattr(runtime, "serialize_state")
            else asdict(plan.proposed_state)
        )
        runtime_state = getattr(runtime, "state", None)
        current_state = (
            runtime.serialize_state(runtime_state)
            if runtime_state is not None and hasattr(runtime, "serialize_state")
            else asdict(runtime_state)
            if runtime_state is not None
            else None
        )
        store.record_decision(
            network,
            symbol,
            decision_id=plan.decision_id,
            action=",".join(action.action.value for action in plan.actions) or "HOLD",
            details={
                "decision_time": plan.decision_time.isoformat(),
                "execution_time": None if plan.execution_time is None else plan.execution_time.isoformat(),
                "capital_limit": capital_limit,
            },
            expected_order_count=len(plan.actions),
            pre_state=current_state,
            proposed_state=proposed_state,
        )
        last_action = ""
        last_order_id: str | None = None
        filled = 0
        try:
            for ordinal, action in enumerate(plan.actions):
                current = executor.current_position(symbol)
                action_value = action.action.value
                if action_value in {
                SarPyramidActionType.OPEN.value,
                SarPyramidActionType.ADD.value,
                }:
                    if action_value == SarPyramidActionType.OPEN.value and current.direction:
                        raise UnsupportedAccountError("open intent requires a flat exchange position")
                    if action_value == SarPyramidActionType.ADD.value and current.direction != action.direction:
                        raise UnsupportedAccountError(
                            "add intent direction does not match the exchange position"
                        )
                    available = executor.available_balance()
                    capital_weight = float(getattr(action, "capital_weight", 0.0))
                    if capital_weight > 0:
                        quantity = executor.weighted_layer_quantity(
                            available_balance=available,
                            capital_limit=capital_limit,
                            reference_price=plan.reference_price or mark_price,
                            capital_weight=capital_weight,
                        )
                    else:
                        quantity = executor.layer_quantity(
                            available_balance=available,
                            capital_limit=capital_limit,
                            reference_price=plan.reference_price or mark_price,
                            layers=runtime.config.layers,
                            target_fraction=runtime.config.target_notional_fraction,
                        )
                    intent_type = (
                        OrderIntentType.OPEN
                        if action_value == SarPyramidActionType.OPEN.value
                        else OrderIntentType.ADD
                    )
                else:
                    if not current.direction:
                        raise UnsupportedAccountError("close intent has no exchange position")
                    if current.direction != action.direction:
                        raise UnsupportedAccountError(
                            "close intent direction does not match the exchange position"
                        )
                    quantity = current.quantity
                    intent_type = OrderIntentType.CLOSE
                intent = OrderIntent(
                    symbol=symbol,
                    action=intent_type,
                    direction=action.direction,
                    quantity=quantity,
                    decision_id=plan.decision_id,
                    ordinal=ordinal,
                )
                self._record_attempt(intent, store=store, network=network)
                try:
                    result = self._execute_intent(executor, intent, network)
                except RecoveryRequiredError as exc:
                    store.record_order_result(
                        network,
                        symbol,
                        decision_id=plan.decision_id,
                        ordinal=ordinal,
                        status="unknown",
                        error=exc,
                    )
                    raise
                except Exception as exc:
                    store.record_order_result(
                        network,
                        symbol,
                        decision_id=plan.decision_id,
                        ordinal=ordinal,
                        status="rejected",
                        error=exc,
                    )
                    raise
                self._record_result(plan.decision_id, ordinal, result, store=store, network=network)
                last_order_id = str(result.order_id) if result.order_id is not None else result.client_order_id
                requested_quantity = getattr(
                    result, "requested_quantity", getattr(result, "quantity", None)
                )
                if result.status != "FILLED" or result.executed_quantity != requested_quantity:
                    raise RecoveryRequiredError("exchange order was not confirmed fully filled")
                filled += 1
                last_action = f"{intent_type.value.upper()} {'LONG' if action.direction > 0 else 'SHORT'}"
        except Exception as exc:
            current_decision = store.load(network, symbol)["decisions"][plan.decision_id]
            has_fill = any(
                order["result"]["status"] == "filled"
                for order in current_decision["orders"].values()
            )
            terminal_status = "recovery_required" if has_fill or isinstance(
                exc, RecoveryRequiredError
            ) else "failed"
            store.transition_decision(
                network,
                symbol,
                decision_id=plan.decision_id,
                status=terminal_status,
                error=exc,
            )
            raise
        store.transition_decision(
            network, symbol, decision_id=plan.decision_id, status="committed"
        )
        runtime.commit(plan)
        final_position = executor.current_position(symbol)
        last_signal = {1: "LONG", -1: "SHORT"}.get(final_position.direction, "NONE")
        return _CycleResult(
            mark_price,
            last_signal,
            last_action,
            filled,
            plan.no_action_reason,
            last_order_id,
        )

    def _apply_cycle(self, result: _CycleResult) -> None:
        self._sar_adx_mark_price = result.mark_price
        self.last_signal = result.last_signal
        if result.last_action:
            self.last_action = result.last_action
        self.no_action_reason = result.no_action_reason
        if result.last_exchange_order_id:
            self.last_exchange_order_id = result.last_exchange_order_id

    def hydrate_persisted_status(self, symbol: str, network: str | None = None) -> None:
        """Load display-only execution counters without inferring a position."""

        if self.running or self._sar_adx_runtime is not None or network not in {"testnet", "mainnet"}:
            return
        normalized = symbol.strip().upper()
        store = ExecutionStore()
        summary = store.status_summary(network, normalized)
        self._cached_symbol = normalized
        self._cached_direction = "NONE"
        if summary is not None:
            self._execution_store = store
            self._network = network
            self._symbol = normalized

    @staticmethod
    def _restored_strategy_state(
        store: ExecutionStore, network: str, symbol: str
    ) -> dict[str, Any] | None:
        document = store.load(network, symbol)
        if document is None:
            return None
        ordered_decisions = sorted(
            document["decisions"].values(),
            key=lambda item: (item["created_at"], item["decision_id"]),
            reverse=True,
        )
        for decision in ordered_decisions:
            if decision.get("status") != "committed":
                continue
            details = decision.get("details", {})
            if decision.get("action") == "CLOSE" and details.get("reason") == "strategy_stop":
                return None
            state = decision.get("proposed_state")
            decision_time = details.get("decision_time")
            if isinstance(state, dict) and decision_time:
                return {
                    "symbol": symbol,
                    "strategy": state,
                    "last_processed_decision_time": decision_time,
                    "last_execution_open_time": details.get("execution_time"),
                }
        return None

    @staticmethod
    def _journal_position(
        store: ExecutionStore, network: str, symbol: str
    ) -> _JournalPosition:
        document = store.load(network, symbol)
        if document is None:
            return _JournalPosition()
        direction = 0
        quantity = Decimal("0")
        layers = 0
        anchor: float | None = None
        ordered_decisions = sorted(
            document["decisions"].values(),
            key=lambda item: (item["created_at"], item["decision_id"]),
        )
        for decision in ordered_decisions:
            for order in sorted(decision["orders"].values(), key=lambda item: item["ordinal"]):
                if order["result"]["status"] != "filled":
                    continue
                request = order["request"]
                action = request.get("action")
                order_quantity = Decimal(str(order["result"].get("filled_quantity") or "0"))
                if action in {"open", "add"}:
                    order_direction = int(request["direction"])
                    if direction not in {0, order_direction}:
                        raise ExecutionStoreError("execution journal direction is inconsistent")
                    direction = order_direction
                    quantity += order_quantity
                    layers += 1
                    price = order["result"].get("details", {}).get("average_price")
                    if price is not None and float(price) > 0:
                        anchor = float(price)
                elif action == "close":
                    direction, quantity, layers, anchor = 0, Decimal("0"), 0, None
        return _JournalPosition(direction, quantity, layers, anchor)

    def _reconcile_execution_journal(
        self,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        symbol: str,
    ) -> None:
        document = store.load(network, symbol)
        if document is None:
            return
        for decision_id, decision in document["decisions"].items():
            if decision["status"] in {"committed", "failed"}:
                continue
            if decision["status"] == "recovery_required":
                raise RecoveryRequiredError(
                    "execution decision requires explicit reconciliation"
                )
            for order in decision["orders"].values():
                if order["result"]["status"] in {"filled", "rejected", "cancelled"}:
                    continue
                request = order["request"]
                intent = OrderIntent(
                    symbol=symbol,
                    action=OrderIntentType(request["action"]),
                    direction=int(request["direction"]),
                    quantity=Decimal(str(request["quantity"])),
                    decision_id=decision_id,
                    ordinal=int(order["ordinal"]),
                )
                try:
                    result = self._lookup_intent(executor, intent, network)
                except Exception as exc:
                    store.transition_decision(
                        network,
                        symbol,
                        decision_id=decision_id,
                        status="recovery_required",
                        error=exc,
                    )
                    raise
                self._record_result(
                    decision_id,
                    intent.ordinal,
                    result,
                    store=store,
                    network=network,
                )
                requested_quantity = getattr(
                    result, "requested_quantity", getattr(result, "quantity", None)
                )
                if result.status != "FILLED" or result.executed_quantity != requested_quantity:
                    store.transition_decision(
                        network,
                        symbol,
                        decision_id=decision_id,
                        status="recovery_required",
                        error="journaled order was not reconciled as fully filled",
                    )
                    raise RecoveryRequiredError(
                        "journaled order was not reconciled as fully filled"
                    )
            refreshed = store.load(network, symbol)["decisions"][decision_id]
            statuses = [order["result"]["status"] for order in refreshed["orders"].values()]
            complete = len(statuses) == refreshed["expected_order_count"]
            if complete and all(status == "filled" for status in statuses):
                store.transition_decision(
                    network, symbol, decision_id=decision_id, status="committed"
                )
            elif any(status == "filled" for status in statuses):
                store.transition_decision(
                    network,
                    symbol,
                    decision_id=decision_id,
                    status="recovery_required",
                    error="partially executed decision requires reconciliation",
                )
                raise RecoveryRequiredError("partially executed decision requires reconciliation")
            else:
                store.transition_decision(
                    network,
                    symbol,
                    decision_id=decision_id,
                    status="failed",
                    error="unexecuted decision was abandoned during restart audit",
                )

    def _record_attempt(
        self,
        intent: OrderIntent,
        *,
        store: ExecutionStore | None = None,
        network: str | None = None,
    ) -> None:
        target = store or self._execution_store
        bound_network = network or self._network
        if target is None or not bound_network:
            raise ExecutionStoreError("execution journal is unavailable")
        target.record_order_attempt(
            bound_network,
            intent.symbol,
            decision_id=intent.decision_id,
            ordinal=intent.ordinal,
            request={
                "action": intent.action.value,
                "direction": intent.direction,
                "quantity": str(intent.quantity),
            },
        )

    @staticmethod
    def _execute_intent(
        executor: ExchangeExecutor, intent: OrderIntent, network: str
    ) -> Any:
        if not all(
            hasattr(executor, attribute) for attribute in ("gateway", "rules", "network")
        ):
            return executor.execute(intent)
        binding = ExchangeBinding("binance", network, intent.symbol)
        adapter = build_binance_adapter(binding, executor.gateway, executor)
        request = OrderRequest(
            intent.symbol,
            OrderAction(intent.action.value),
            intent.direction,
            intent.quantity,
            intent.decision_id,
            intent.ordinal,
        )
        return adapter.trading.execute(request, ExecutionAuthorization.issue(binding))

    @staticmethod
    def _lookup_intent(
        executor: ExchangeExecutor, intent: OrderIntent, network: str
    ) -> Any:
        if not all(
            hasattr(executor, attribute) for attribute in ("gateway", "rules", "network")
        ):
            return executor.lookup(intent)
        binding = ExchangeBinding("binance", network, intent.symbol)
        adapter = build_binance_adapter(binding, executor.gateway, executor)
        request = OrderRequest(
            intent.symbol,
            OrderAction(intent.action.value),
            intent.direction,
            intent.quantity,
            intent.decision_id,
            intent.ordinal,
        )
        return adapter.trading.lookup(request, ExecutionAuthorization.issue(binding))

    def _record_result(
        self,
        decision_id: str,
        ordinal: int,
        result,
        *,
        store: ExecutionStore | None = None,
        network: str | None = None,
    ) -> None:
        target = store or self._execution_store
        bound_network = network or self._network
        if target is None or not bound_network:
            raise ExecutionStoreError("execution journal is unavailable")
        status = {
            "FILLED": "filled",
            "PARTIALLY_FILLED": "partially_filled",
            "NEW": "submitted",
            "REJECTED": "rejected",
            "EXPIRED": "rejected",
            "CANCELED": "cancelled",
        }.get(result.status, "unknown")
        target.record_order_result(
            bound_network,
            result.symbol,
            decision_id=decision_id,
            ordinal=ordinal,
            status=status,
            exchange_order_id=result.order_id,
            client_order_id=result.client_order_id,
            filled_quantity=str(result.executed_quantity),
            details={"average_price": str(result.average_price)},
        )
        self._capture_analytics_order(decision_id, ordinal, result)

    def _capture_analytics_run(
        self,
        client: Any,
        store: ExecutionStore,
        network: str,
        symbol: str,
        run_id: str,
        capital_limit: float,
        *,
        strategy_type: str,
        config_version: str,
        resume_existing: bool = False,
    ) -> None:
        """Persist run ownership after validation and before any order can be sent."""
        try:
            api_key = getattr(client, "API_KEY", None)
            if not isinstance(api_key, str) or not api_key:
                return
            service = StrategyAnalyticsService()
            analytics_run_id = f"{run_id}:{uuid4().hex}"
            scope_id, analytics_run_id = service.capture_run(
                account_fingerprint(api_key),
                network,
                symbol,
                run_id=analytics_run_id,
                strategy_type=strategy_type,
                config_version=config_version,
                allocation_equity=capital_limit,
                execution_store=store,
                resume_existing=resume_existing,
            )
            self._analytics_service = service
            self._analytics_scope_id = scope_id
            self._analytics_run_id = analytics_run_id
        except Exception as exc:
            self._analytics_service = None
            self._analytics_scope_id = None
            self._analytics_run_id = None
            raise ExecutionStoreError(
                "analytics run identity could not be persisted before execution"
            ) from exc

    def _capture_analytics_order(self, decision_id: str, ordinal: int, result: Any) -> None:
        if self._analytics_service is None or self._analytics_scope_id is None or self._analytics_run_id is None:
            return
        try:
            self._analytics_service.capture_order(
                self._analytics_scope_id,
                self._analytics_run_id,
                decision_id,
                ordinal,
                exchange_order_id=result.order_id,
                client_order_id=result.client_order_id,
            )
        except Exception:
            pass

    def _clear_failure_state(self) -> None:
        self.failure_count = 0
        self.next_retry_at = None
        self.error_code = None
        self.error_msg = ""
        self.last_success_at = datetime.now(timezone.utc).isoformat()

    def _observe_gateway_failure(self, exc: Exception) -> None:
        if not isinstance(exc, BinanceGatewayError) or exc.failure is None:
            return
        self.failure_count = (
            BinanceRetryPolicy().max_attempts if exc.failure.retryable else 1
        )
        self.error_code = exc.code
        self.error_msg = str(exc)
        self.next_retry_at = None

    def _observe_retry_delay(self, delay: float) -> None:
        self.failure_count += 1
        self.engine_state = "retrying"
        self.error_code = "binance_read_retry"
        self.error_msg = "Binance read retry is in progress"
        self.next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        time.sleep(delay)

    def _set_terminal_error(self, exc: Exception) -> None:
        cause = exc.cause if isinstance(exc, _SnapshotFetchError) else exc
        self.next_retry_at = None
        if isinstance(cause, BinanceGatewayError) and cause.failure and cause.failure.retryable:
            self.engine_state = "network_halted"
            self.error_code = cause.code
            self.error_msg = str(cause)
        elif isinstance(
            cause,
            (
                LiveStrategyRuntimeError,
                ExchangeExecutionError,
                ExecutionStoreError,
            ),
        ):
            self.engine_state = "recovery_required"
            self.error_code = "runtime_recovery_required"
            self.error_msg = "Strategy execution requires reconciliation"
        else:
            self.engine_state = "halted"
            self.error_code = "engine_failure"
            self.error_msg = "Strategy stopped unexpectedly"

    @staticmethod
    def _log_snapshot_failure(
        symbol: str,
        exc: _SnapshotFetchError,
    ) -> None:
        cause = exc.cause
        logger.warning(
            "SAR/ADX snapshot retry symbol={} stage={} type={} status={} code={}",
            symbol,
            exc.stage,
            type(cause).__name__,
            getattr(getattr(cause, "failure", None), "status_code", None),
            getattr(cause, "code", None),
        )

    @staticmethod
    def _log_terminal_error(symbol: str, exc: Exception) -> None:
        stage = exc.stage if isinstance(exc, _SnapshotFetchError) else "runtime"
        cause = exc.cause if isinstance(exc, _SnapshotFetchError) else exc
        logger.error(
            "SAR/ADX engine halted symbol={} stage={} type={} status={} code={}",
            symbol,
            stage,
            type(cause).__name__,
            getattr(getattr(cause, "failure", None), "status_code", None),
            getattr(cause, "code", None),
        )

    def _same_runtime(self, cfg: dict) -> bool:
        return (
            cfg.get("strategy_type") == self._strategy_type
            and cfg.get("config_version") == self._config_version
            and str(cfg.get("config_hash") or "legacy") == self._config_hash
            and str(cfg.get("symbol", "")).upper() == self._symbol
            and str(cfg.get("network", "")) == self._network
            and float(cfg.get("capital_limit", 0.0)) == self._capital_limit
            and float(cfg.get("check_interval", 15.0)) == self._check_interval
        )

    @staticmethod
    def _validate_config(cfg: dict) -> tuple[str, float, float, str]:
        strategy_type = str(cfg.get("strategy_type", ""))
        expected_version = SUPPORTED_STRATEGY_VERSIONS.get(strategy_type)
        if expected_version is None:
            raise ValueError(f"unsupported production strategy: {strategy_type}")
        if cfg.get("config_version") != expected_version:
            raise ValueError(f"unsupported strategy config version: {cfg.get('config_version')}")
        if cfg.get("interval", BAR_INTERVAL) != BAR_INTERVAL:
            raise ValueError("strategy runtime requires the 5m interval")
        if strategy_type != STRATEGY_TYPE:
            config_hash = cfg.get("config_hash")
            if (
                not isinstance(config_hash, str)
                or len(config_hash) != 64
                or any(character not in "0123456789abcdef" for character in config_hash)
            ):
                raise ValueError("strategy configuration hash is invalid")
            if not isinstance(cfg.get("parameters"), dict):
                raise ValueError("strategy parameters are required")
            expected_hash = configuration_hash(
                strategy_type,
                expected_version,
                cfg["parameters"],
            )
            if config_hash != expected_hash:
                raise ValueError("strategy configuration hash does not match parameters")

        symbol = str(cfg.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        capital_limit = float(cfg.get("capital_limit", 0.0))
        if not math.isfinite(capital_limit) or capital_limit <= 0.0:
            raise ValueError("capital limit must be positive")
        network = str(cfg.get("network", ""))
        if network not in {"testnet", "mainnet"}:
            raise ValueError("execution network is invalid")
        check_interval = float(cfg.get("check_interval", 15.0))
        if not math.isfinite(check_interval) or check_interval <= 0.0:
            raise ValueError("check interval must be positive")
        return symbol, capital_limit, check_interval, network

    @staticmethod
    def _build_runtime(
        symbol: str,
        cfg: dict,
        restored: dict[str, Any] | None,
    ) -> Any:
        strategy_type = str(cfg["strategy_type"])
        if strategy_type in {"sar_martingale", "sar_anti_martingale"}:
            return LiveLayeredStrategyRuntime(
                symbol,
                strategy_type=strategy_type,
                config_version=str(cfg["config_version"]),
                parameters=cfg["parameters"],
                restored_payload=restored,
            )
        if strategy_type == "sar_adx_trend":
            parameters = cfg["parameters"]
            config = SarPyramidConfig(
                target_notional_fraction=1.0,
                layers=int(parameters["max_layers"]),
                sar_step=float(parameters["sar_step"]),
                sar_max=float(parameters["sar_max"]),
                use_adx_filter=True,
                adx_timeframe=str(parameters["adx_timeframe"]),
                adx_period=int(parameters["adx_period"]),
                adx_threshold=float(parameters["adx_threshold"]),
                adx_rising_periods=int(parameters["adx_rising_periods"]),
                entry_confirmation_bars=int(parameters["entry_confirmation_bars"]),
                recapture_buffer_fraction=float(parameters["recapture_buffer_fraction"]),
                require_progressive_adds=True,
                max_entries_per_adx_regime=int(parameters["max_entries_per_adx_regime"]),
            )
            return LiveStrategyRuntime(
                symbol,
                restored_payload=restored,
                config=config,
                config_version=str(cfg["config_version"]),
            )
        return LiveStrategyRuntime(symbol, restored_payload=restored)

    @staticmethod
    def _is_tradable(exchange_info: dict, symbol: str) -> bool:
        return any(
            item.get("symbol") == symbol and item.get("status") == "TRADING"
            for item in exchange_info.get("symbols", [])
        )

    async def _reap_terminal_task(self) -> None:
        task = self._task
        if task is None or not task.done():
            return
        try:
            try:
                await self._await_terminal(task)
            except Exception:
                pass
        finally:
            if self._task is task and task.done():
                self._clear_runtime()

    def _clear_runtime(self) -> None:
        self.running = False
        self.engine_state = "stopped"
        self._task = None
        self._sar_adx_runtime = None
        self._executor = None
        self._client = None
        self._sar_adx_mark_price = None
        self._check_interval = None
        self._strategy_name = ""
        self._strategy_type = ""
        self._config_version = ""
        self._config_hash = ""
        self._symbol = ""
        self._analytics_service = None
        self._analytics_scope_id = None
        self._analytics_run_id = None
        self._intent_store = None
        self._trading_lease = None
        self._runtime_scope = None
        self._runtime_config = None

    def _renew_trading_lease(self) -> None:
        if self._intent_store is None or self._trading_lease is None:
            return
        self._trading_lease = self._intent_store.renew_lease(
            self._trading_lease, ttl_seconds=60
        )

    def _release_trading_lease(self) -> None:
        if self._intent_store is None or self._trading_lease is None:
            return
        try:
            self._intent_store.release_lease(self._trading_lease)
        finally:
            self._trading_lease = None

    def _record_stopped_intent(self) -> None:
        if (
            self._intent_store is None
            or self._runtime_scope is None
            or self._runtime_config is None
        ):
            return
        self._intent_store.request_stop(self._runtime_scope, self._runtime_config)

    @staticmethod
    async def _await_terminal(task: asyncio.Task[None]) -> None:
        """Wait for a worker without mistaking caller cancellation for its exit."""

        current = asyncio.current_task()
        if current is None:
            await asyncio.shield(task)
            return
        cancellation_count = current.cancelling()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if current.cancelling() == cancellation_count:
                if task.cancelled():
                    return
                raise
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.result()
        finally:
            if current.cancelling() != cancellation_count:
                raise asyncio.CancelledError


bot_engine = BotEngine()
