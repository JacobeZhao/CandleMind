"""Transactional coordinator for the production SAR/ADX paper runtime."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Callable

import pandas as pd
from loguru import logger
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout

from .paper_broker import PaperBroker
from .sar_adx_runtime import SarAdxPaperRuntime, SarAdxRuntimeError
from .sar_adx_state_store import SarAdxStateError, SarAdxStateStore


STRATEGY_TYPE = "sar_adx_pyramid"
CONFIG_VERSION = "sar_adx_v3"
BAR_INTERVAL = "5m"


@dataclass(frozen=True)
class _CycleResult:
    mark_price: float
    last_signal: str
    last_action: str
    fill_count: int


@dataclass(frozen=True)
class _MarketSnapshot:
    klines: list
    server_time: dict
    funding: list
    exchange_info: dict
    mark_price: dict


class _SnapshotFetchError(Exception):
    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


class BotEngine:
    """Own one SAR/ADX paper runtime and its polling task."""

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
        self.paper = True
        self._paper_cap = 10_000.0
        self.circuit_open = False
        self._sar_adx_runtime: SarAdxPaperRuntime | None = None
        self._sar_adx_mark_price: float | None = None
        self._check_interval: float | None = None
        self._cached_symbol = ""
        self._cached_direction: str | None = None
        self._cached_fill_count: int | None = None
        self._cached_fill_count_complete = False

    @property
    def status(self) -> dict:
        runtime_status: dict[str, Any] = {}
        if self._sar_adx_runtime is not None:
            mark_price = self._sar_adx_mark_price or 1.0
            runtime_status = self._sar_adx_runtime.status(mark_price)
            self._cache_runtime_status(runtime_status)
        paper_fill_count = runtime_status.get(
            "paper_fill_count", self._cached_fill_count
        )
        direction = runtime_status.get("direction")
        if direction is not None:
            position_direction = {1: "LONG", -1: "SHORT"}.get(direction, "NONE")
        else:
            position_direction = self._cached_direction
        status = {
            "running": self.running,
            "engine_state": self.engine_state,
            "last_signal": position_direction or "NONE",
            "position_direction": position_direction,
            "last_action": self.last_action,
            "trade_count": paper_fill_count,
            "paper_fill_count": paper_fill_count,
            "paper_fill_count_complete": runtime_status.get(
                "paper_fill_count_complete", self._cached_fill_count_complete
            ),
            "error": self.error_msg,
            "error_code": self.error_code,
            "failure_count": self.failure_count,
            "last_success_at": self.last_success_at,
            "next_retry_at": self.next_retry_at,
            "strategy_name": self._strategy_name,
            "strategy_type": self._strategy_type,
            "config_version": self._config_version,
            "symbol": self._symbol,
            "paper": self.paper,
            "paper_equity": round(self._paper_cap, 2) if self.paper else None,
            "circuit_open": self.circuit_open,
        }
        if runtime_status:
            status.update(runtime_status)
            status["paper_equity"] = round(status["paper_equity"], 2)
        return status

    async def start(self, client, cfg: dict) -> None:
        """Start only after all fallible initialization has completed."""

        symbol, initial_capital, check_interval = self._validate_config(cfg)
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

            runtime = SarAdxPaperRuntime(symbol, initial_cash=initial_capital)
            first_cycle = await self._cycle(
                client,
                symbol,
                runtime,
                allow_flat_rebaseline=True,
            )

            task: asyncio.Task[None] | None = None
            try:
                task = asyncio.create_task(
                    self._loop(client, symbol, runtime, check_interval),
                    name=f"sar-adx-paper:{symbol}",
                )
                self.error_msg = ""
                self._strategy_name = cfg.get("name", "SAR + ADX Pyramid V3")
                self._strategy_type = STRATEGY_TYPE
                self._config_version = CONFIG_VERSION
                self._symbol = symbol
                self.paper = True
                self._paper_cap = initial_capital
                self.circuit_open = False
                self._sar_adx_runtime = runtime
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
                "Engine started: {} {} [{}] PAPER",
                symbol,
                BAR_INTERVAL,
                STRATEGY_TYPE,
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
            finally:
                if self._task is task and task.done():
                    self._clear_runtime()
            logger.info("Engine stopped")

    async def _loop(
        self,
        client,
        symbol: str,
        runtime: SarAdxPaperRuntime,
        check_interval: float,
    ) -> None:
        task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(check_interval)
                result = await self._cycle_with_retry(client, symbol, runtime)
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
        runtime: SarAdxPaperRuntime | None = None,
        *,
        allow_flat_rebaseline: bool = False,
    ) -> _CycleResult:
        """Advance the paper runtime from completed Binance 5m bars."""

        runtime = runtime or self._sar_adx_runtime
        if runtime is None:
            raise RuntimeError("SAR/ADX runtime is not initialized")

        try:
            snapshot = await self._fetch_snapshot(client, symbol)
        except _SnapshotFetchError as exc:
            raise exc.cause from exc
        return self._process_snapshot(
            snapshot,
            symbol,
            runtime,
            allow_flat_rebaseline=allow_flat_rebaseline,
        )

    async def _cycle_with_retry(
        self,
        client,
        symbol: str,
        runtime: SarAdxPaperRuntime,
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
        result = self._process_snapshot(snapshot, symbol, runtime)
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
        runtime: SarAdxPaperRuntime,
        *,
        allow_flat_rebaseline: bool = False,
    ) -> _CycleResult:
        raw = snapshot.klines
        server = snapshot.server_time
        funding_raw = snapshot.funding
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

        fills = runtime.process_bars(
            bars,
            server_time=pd.Timestamp(int(server["serverTime"]), unit="ms", tz="UTC"),
            funding=pd.DataFrame(
                {
                    "funding_time": [item["fundingTime"] for item in funding_raw],
                    "funding_rate": [item["fundingRate"] for item in funding_raw],
                }
            ),
            execution_price=float(mark["markPrice"]),
            eligible=self._is_tradable(exchange_info, symbol),
            allow_flat_rebaseline=allow_flat_rebaseline,
        )
        mark_price = float(bars.iloc[-1]["close"])
        runtime_status = runtime.status(mark_price)
        last_signal = {1: "LONG", -1: "SHORT"}.get(
            runtime_status["direction"], "NONE"
        )
        if fills:
            latest = fills[-1]
            direction = "LONG" if latest.direction == 1 else "SHORT"
            last_action = (
                f"[SAR+ADX paper] {latest.action} {direction} "
                f"{latest.quantity:.6f} @ {latest.fill_price:.6f}"
            )
        else:
            last_action = ""
        return _CycleResult(mark_price, last_signal, last_action, len(fills))

    def _apply_cycle(self, result: _CycleResult) -> None:
        self._sar_adx_mark_price = result.mark_price
        self.last_signal = result.last_signal
        if result.last_action:
            self.last_action = result.last_action

    def hydrate_persisted_status(self, symbol: str) -> None:
        """Load display-only paper status while no runtime is active."""

        symbol = symbol.strip().upper()
        if self._sar_adx_runtime is not None or self.running:
            return
        if self._cached_symbol == symbol and self._cached_direction is not None:
            return
        try:
            payload = SarAdxStateStore().load_summary(
                symbol,
                config_version=CONFIG_VERSION,
            )
            if payload is None:
                self._cached_symbol = symbol
                self._cached_direction = "NONE"
                self._cached_fill_count = 0
                self._cached_fill_count_complete = True
                return
            broker = PaperBroker.from_dict(payload["broker"])
        except (SarAdxStateError, KeyError, TypeError, ValueError) as exc:
            self._cached_symbol = symbol
            self._cached_direction = None
            self._cached_fill_count = None
            self._cached_fill_count_complete = False
            self.engine_state = "recovery_required"
            self.error_code = "state_recovery_required"
            self.error_msg = "Paper strategy state requires recovery"
            logger.error(
                "SAR/ADX persisted status unavailable symbol={} type={}",
                symbol,
                type(exc).__name__,
            )
            return
        self._cached_symbol = symbol
        self._cached_direction = {1: "LONG", -1: "SHORT"}.get(
            broker.position.direction,
            "NONE",
        )
        self._cached_fill_count_complete = broker.paper_fill_count_complete
        self._cached_fill_count = (
            broker.paper_fill_count if broker.paper_fill_count_complete else None
        )
        self.last_signal = self._cached_direction

    def _cache_runtime_status(self, runtime_status: dict[str, Any]) -> None:
        direction = runtime_status.get("direction")
        self._cached_symbol = str(runtime_status.get("symbol") or self._symbol)
        self._cached_direction = {1: "LONG", -1: "SHORT"}.get(direction, "NONE")
        self._cached_fill_count_complete = bool(
            runtime_status.get("paper_fill_count_complete", False)
        )
        self._cached_fill_count = runtime_status.get("paper_fill_count")

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
        elif isinstance(cause, (SarAdxRuntimeError, SarAdxStateError)):
            self.engine_state = "recovery_required"
            self.error_code = "runtime_recovery_required"
            self.error_msg = "Paper strategy state requires recovery"
        else:
            self.engine_state = "halted"
            self.error_code = "engine_failure"
            self.error_msg = "Paper strategy stopped unexpectedly"

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
            and bool(cfg.get("paper", True))
            and float(cfg.get("initial_capital", 10_000.0)) == self._paper_cap
            and float(cfg.get("check_interval", 15.0)) == self._check_interval
        )

    @staticmethod
    def _validate_config(cfg: dict) -> tuple[str, float, float]:
        if not bool(cfg.get("paper", True)):
            raise ValueError(
                "live trading requires explicit server authorization; "
                "SAR/ADX runtime is paper-only"
            )
        if cfg.get("strategy_type") != STRATEGY_TYPE:
            raise ValueError(f"unsupported production strategy: {cfg.get('strategy_type')}")
        if cfg.get("config_version") != CONFIG_VERSION:
            raise ValueError(f"unsupported SAR/ADX config version: {cfg.get('config_version')}")
        if cfg.get("interval", BAR_INTERVAL) != BAR_INTERVAL:
            raise ValueError("SAR/ADX runtime requires the 5m interval")

        symbol = str(cfg.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        initial_capital = float(cfg.get("initial_capital", 10_000.0))
        if not math.isfinite(initial_capital) or initial_capital <= 0.0:
            raise ValueError("initial capital must be positive")
        check_interval = float(cfg.get("check_interval", 15.0))
        if not math.isfinite(check_interval) or check_interval <= 0.0:
            raise ValueError("check interval must be positive")
        return symbol, initial_capital, check_interval

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
        if self._sar_adx_runtime is not None and hasattr(self._sar_adx_runtime, "status"):
            mark_price = self._sar_adx_mark_price or 1.0
            self._cache_runtime_status(self._sar_adx_runtime.status(mark_price))
        self.running = False
        self.engine_state = "stopped"
        self._task = None
        self._sar_adx_runtime = None
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
