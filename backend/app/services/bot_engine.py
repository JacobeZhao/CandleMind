"""Transactional coordinator for exchange-backed trend strategy execution."""

from __future__ import annotations

import asyncio
import math
from decimal import Decimal
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Callable

import pandas as pd
from loguru import logger
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.strategies.sar_pyramid import PositionSnapshot, SarPyramidActionType

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
from .live_strategy_runtime import (
    DecisionPlan,
    LiveStrategyRuntime,
    LiveStrategyRuntimeError,
)


STRATEGY_TYPE = "sar_adx_pyramid"
CONFIG_VERSION = "sar_adx_v3"
BAR_INTERVAL = "5m"


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
    server_time: dict
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

    _MAX_SNAPSHOT_ATTEMPTS = 3
    _RETRY_DELAYS = (0.5, 1.0)
    _MAX_RETRY_AFTER_SECONDS = 5.0
    _MAX_RETRY_BUDGET_SECONDS = 5.0

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
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

            exchange_info = await asyncio.to_thread(client.futures_exchange_info)
            if not self._is_tradable(exchange_info, symbol):
                raise ValueError(f"symbol is not currently tradable: {symbol}")

            executor = ExchangeExecutor(client, SymbolRules.from_exchange_info(exchange_info, symbol))
            store = ExecutionStore()
            run_id = f"cm-{network}-{symbol}-{CONFIG_VERSION}"
            store.initialize(
                network,
                symbol,
                run_id=run_id,
                metadata={"strategy_type": STRATEGY_TYPE, "config_version": CONFIG_VERSION},
            )
            await asyncio.to_thread(
                self._reconcile_execution_journal,
                executor,
                store,
                network,
                symbol,
            )
            restored = self._restored_strategy_state(store, network, symbol)
            runtime = LiveStrategyRuntime(symbol, restored_payload=restored)
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
            if await asyncio.to_thread(executor.available_balance) <= 0:
                raise UnsupportedAccountError("available USDT balance is zero")
            first_cycle = await self._cycle(
                client,
                symbol,
                runtime,
                executor=executor,
                store=store,
                network=network,
                capital_limit=capital_limit,
            )

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
                self._strategy_type = STRATEGY_TYPE
                self._config_version = CONFIG_VERSION
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
                STRATEGY_TYPE,
                network,
            )

    async def stop(self) -> None:
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
            try:
                await self._await_terminal(task)
                await self._close_exchange_position("strategy_stop")
            finally:
                if self._task is task and task.done():
                    self._clear_runtime()
            logger.info("Engine stopped")

    async def _close_exchange_position(self, reason: str) -> None:
        if not self._executor or not self._execution_store or not self._symbol:
            return
        position = await asyncio.to_thread(self._executor.current_position, self._symbol)
        if not position.direction:
            return
        decision_id = f"stop:{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        self._execution_store.record_decision(
            self._network,
            self._symbol,
            decision_id=decision_id,
            action="CLOSE",
            details={"reason": reason},
        )
        intent = OrderIntent(
            symbol=self._symbol,
            action=OrderIntentType.CLOSE,
            direction=position.direction,
            quantity=position.quantity,
            decision_id=decision_id,
        )
        self._record_attempt(intent)
        result = await asyncio.to_thread(self._executor.execute, intent)
        self._record_result(decision_id, 0, result)
        if result.status != "FILLED":
            raise RecoveryRequiredError("stop order was not confirmed filled")

    async def _loop(
        self,
        client,
        symbol: str,
        runtime: LiveStrategyRuntime,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        capital_limit: float,
        check_interval: float,
    ) -> None:
        task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(check_interval)
                result = await self._cycle_with_retry(
                    client, symbol, runtime, executor, store, network, capital_limit
                )
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
        runtime: LiveStrategyRuntime | None = None,
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
        return await asyncio.to_thread(
            self._process_snapshot, snapshot, symbol, runtime, executor, store,
            network, capital_limit,
        )

    async def _cycle_with_retry(
        self,
        client,
        symbol: str,
        runtime: LiveStrategyRuntime,
        executor: ExchangeExecutor,
        store: ExecutionStore,
        network: str,
        capital_limit: float,
    ) -> _CycleResult:
        snapshot: _MarketSnapshot | None = None
        retry_started = monotonic()
        for attempt in range(1, self._MAX_SNAPSHOT_ATTEMPTS + 1):
            try:
                snapshot = await self._fetch_snapshot(client, symbol)
                break
            except _SnapshotFetchError as exc:
                retry = self._retry_details(exc.cause)
                if retry is None or attempt >= self._MAX_SNAPSHOT_ATTEMPTS:
                    if retry is not None:
                        self.failure_count = attempt
                    raise
                self.failure_count = attempt
                self.engine_state = "retrying"
                self.error_code = retry
                self.error_msg = "Binance is temporarily unavailable"
                delay = self._retry_delay(attempt, exc.cause)
                if (
                    delay is None
                    or monotonic() - retry_started + delay
                    > self._MAX_RETRY_BUDGET_SECONDS
                ):
                    raise
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self.next_retry_at = retry_at.isoformat()
                self._log_snapshot_failure(symbol, exc)
                await asyncio.sleep(delay)

        if snapshot is None:
            raise RuntimeError("market snapshot retry invariant failed")
        result = await asyncio.to_thread(
            self._process_snapshot, snapshot, symbol, runtime, executor, store,
            network, capital_limit,
        )
        self.engine_state = "running"
        self._clear_failure_state()
        return result

    async def _fetch_snapshot(self, client, symbol: str) -> _MarketSnapshot:
        batch = asyncio.gather(
            self._fetch_stage(
                "klines",
                client.futures_klines,
                symbol=symbol,
                interval=BAR_INTERVAL,
                limit=500,
            ),
            self._fetch_stage("server_time", client.futures_time),
            self._fetch_stage(
                "funding_rate",
                client.futures_funding_rate,
                symbol=symbol,
                limit=100,
            ),
            self._fetch_stage("exchange_info", client.futures_exchange_info),
            self._fetch_stage(
                "mark_price", client.futures_mark_price, symbol=symbol
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
                and self._retry_details(failure.cause) is None
            ):
                raise failure
        if failures:
            raise failures[0]
        raw, server, funding_raw, exchange_info, mark = results
        return _MarketSnapshot(raw, server, funding_raw, exchange_info, mark)

    @staticmethod
    async def _fetch_stage(
        stage: str,
        request: Callable[..., Any],
        **kwargs: Any,
    ) -> Any:
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
        runtime: LiveStrategyRuntime,
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
            server_time=pd.Timestamp(int(server["serverTime"]), unit="ms", tz="UTC"),
            execution_price=float(mark["markPrice"]),
            eligible=self._is_tradable(exchange_info, symbol),
        )
        mark_price = float(bars.iloc[-1]["close"])
        if plan is None:
            direction = {1: "LONG", -1: "SHORT"}.get(exchange_position.direction, "NONE")
            return _CycleResult(mark_price, direction, "", 0, "bar_already_processed")

        store.record_decision(
            network,
            symbol,
            decision_id=plan.decision_id,
            action=",".join(action.action.value for action in plan.actions) or "HOLD",
            details={
                "strategy_state": asdict(plan.proposed_state),
                "decision_time": plan.decision_time.isoformat(),
                "execution_time": None if plan.execution_time is None else plan.execution_time.isoformat(),
                "capital_limit": capital_limit,
                "expected_order_count": len(plan.actions),
            },
        )
        last_action = ""
        last_order_id: str | None = None
        filled = 0
        for ordinal, action in enumerate(plan.actions):
            current = executor.current_position(symbol)
            if action.action in {SarPyramidActionType.OPEN, SarPyramidActionType.ADD}:
                available = executor.available_balance()
                quantity = executor.layer_quantity(
                    available_balance=available,
                    capital_limit=capital_limit,
                    reference_price=plan.reference_price or mark_price,
                    layers=runtime.config.layers,
                    target_fraction=runtime.config.target_notional_fraction,
                )
                intent_type = (
                    OrderIntentType.OPEN
                    if action.action is SarPyramidActionType.OPEN
                    else OrderIntentType.ADD
                )
            else:
                if not current.direction:
                    raise UnsupportedAccountError("close intent has no exchange position")
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
                result = executor.execute(intent)
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
            if result.status != "FILLED" or result.executed_quantity != result.quantity:
                raise RecoveryRequiredError("exchange order was not confirmed fully filled")
            filled += 1
            last_action = f"{intent_type.value.upper()} {'LONG' if action.direction > 0 else 'SHORT'}"
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
        for decision in reversed(list(document["decisions"].values())):
            orders = list(decision["orders"].values())
            expected = decision.get("details", {}).get("expected_order_count")
            if expected is None or len(orders) != int(expected):
                continue
            if any(order["result"]["status"] != "filled" for order in orders):
                continue
            details = decision.get("details", {})
            state = details.get("strategy_state")
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
        for decision in document["decisions"].values():
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
                result = executor.lookup(intent)
                self._record_result(
                    decision_id,
                    intent.ordinal,
                    result,
                    store=store,
                    network=network,
                )
                if result.status != "FILLED" or result.executed_quantity != result.quantity:
                    raise RecoveryRequiredError(
                        "journaled order was not reconciled as fully filled"
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

    def _clear_failure_state(self) -> None:
        self.failure_count = 0
        self.next_retry_at = None
        self.error_code = None
        self.error_msg = ""
        self.last_success_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _http_status(exc: Exception) -> int | None:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        return status if isinstance(status, int) else None

    @classmethod
    def _retry_details(cls, exc: Exception) -> str | None:
        if isinstance(exc, RequestsSSLError):
            return None
        if isinstance(
            exc,
            (RequestsTimeout, RequestsConnectionError, TimeoutError, ConnectionError),
        ):
            return "network_unavailable"
        status = cls._http_status(exc)
        if status == 408:
            return "request_timeout"
        if status == 429:
            return "rate_limited"
        if status is not None and 500 <= status <= 599:
            return "upstream_unavailable"
        return None

    @classmethod
    def _retry_delay(cls, attempt: int, exc: Exception) -> float | None:
        delay = cls._RETRY_DELAYS[attempt - 1]
        if cls._http_status(exc) != 429:
            return delay
        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
        retry_after = headers.get("Retry-After")
        if retry_after is None:
            return delay
        try:
            requested_delay = float(retry_after)
        except (TypeError, ValueError):
            return delay
        if requested_delay < 0 or requested_delay > cls._MAX_RETRY_AFTER_SECONDS:
            return None
        return max(delay, requested_delay)

    def _set_terminal_error(self, exc: Exception) -> None:
        cause = exc.cause if isinstance(exc, _SnapshotFetchError) else exc
        retry_code = self._retry_details(cause)
        self.next_retry_at = None
        if retry_code is not None:
            self.engine_state = "network_halted"
            self.error_code = retry_code
            self.error_msg = "Binance is temporarily unavailable"
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

    @classmethod
    def _log_snapshot_failure(
        cls,
        symbol: str,
        exc: _SnapshotFetchError,
    ) -> None:
        cause = exc.cause
        logger.warning(
            "SAR/ADX snapshot retry symbol={} stage={} type={} status={} code={}",
            symbol,
            exc.stage,
            type(cause).__name__,
            cls._http_status(cause),
            getattr(cause, "code", None),
        )

    @classmethod
    def _log_terminal_error(cls, symbol: str, exc: Exception) -> None:
        stage = exc.stage if isinstance(exc, _SnapshotFetchError) else "runtime"
        cause = exc.cause if isinstance(exc, _SnapshotFetchError) else exc
        logger.error(
            "SAR/ADX engine halted symbol={} stage={} type={} status={} code={}",
            symbol,
            stage,
            type(cause).__name__,
            cls._http_status(cause),
            getattr(cause, "code", None),
        )

    def _same_runtime(self, cfg: dict) -> bool:
        return (
            cfg.get("strategy_type") == STRATEGY_TYPE
            and cfg.get("config_version") == CONFIG_VERSION
            and str(cfg.get("symbol", "")).upper() == self._symbol
            and str(cfg.get("network", "")) == self._network
            and float(cfg.get("capital_limit", 0.0)) == self._capital_limit
            and float(cfg.get("check_interval", 15.0)) == self._check_interval
        )

    @staticmethod
    def _validate_config(cfg: dict) -> tuple[str, float, float, str]:
        if cfg.get("strategy_type") != STRATEGY_TYPE:
            raise ValueError(f"unsupported production strategy: {cfg.get('strategy_type')}")
        if cfg.get("config_version") != CONFIG_VERSION:
            raise ValueError(f"unsupported SAR/ADX config version: {cfg.get('config_version')}")
        if cfg.get("interval", BAR_INTERVAL) != BAR_INTERVAL:
            raise ValueError("SAR/ADX runtime requires the 5m interval")

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
        self._symbol = ""

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
