"""Transactional coordinator for the production SAR/ADX paper runtime."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

import pandas as pd
from loguru import logger

from .sar_adx_runtime import SarAdxPaperRuntime


STRATEGY_TYPE = "sar_adx_pyramid"
CONFIG_VERSION = "sar_adx_v3"
BAR_INTERVAL = "5m"


@dataclass(frozen=True)
class _CycleResult:
    mark_price: float
    last_signal: str
    last_action: str
    fill_count: int


class BotEngine:
    """Own one SAR/ADX paper runtime and its polling task."""

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.last_signal = "NONE"
        self.last_action = ""
        self.trade_count = 0
        self.error_msg = ""
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

    @property
    def status(self) -> dict:
        status = {
            "running": self.running,
            "last_signal": self.last_signal,
            "last_action": self.last_action,
            "trade_count": self.trade_count,
            "error": self.error_msg,
            "strategy_name": self._strategy_name,
            "strategy_type": self._strategy_type,
            "config_version": self._config_version,
            "symbol": self._symbol,
            "paper": self.paper,
            "paper_equity": round(self._paper_cap, 2) if self.paper else None,
            "circuit_open": self.circuit_open,
        }
        if self._sar_adx_runtime is not None:
            mark_price = self._sar_adx_mark_price or 1.0
            status.update(self._sar_adx_runtime.status(mark_price))
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
            first_cycle = await self._cycle(client, symbol, runtime)

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
                if first_cycle is not None:
                    self._apply_cycle(first_cycle)
                self.running = True
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
                return

            self.running = False
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
                result = await self._cycle(client, symbol, runtime)
                if self._task is not task:
                    return
                self.error_msg = ""
                if result is not None:
                    self._apply_cycle(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._task is task:
                self.error_msg = str(exc)
                self.last_action = "[SAR+ADX paper] halted: recovery required"
            logger.exception("SAR/ADX engine cycle failed")
        finally:
            if self._task is task:
                self.running = False

    async def _cycle(
        self,
        client,
        symbol: str,
        runtime: SarAdxPaperRuntime | None = None,
    ) -> _CycleResult:
        """Advance the paper runtime from completed Binance 5m bars."""

        runtime = runtime or self._sar_adx_runtime
        if runtime is None:
            raise RuntimeError("SAR/ADX runtime is not initialized")

        raw, server, funding_raw, exchange_info, mark = await asyncio.gather(
            asyncio.to_thread(
                client.futures_klines,
                symbol=symbol,
                interval=BAR_INTERVAL,
                limit=500,
            ),
            asyncio.to_thread(client.futures_time),
            asyncio.to_thread(client.futures_funding_rate, symbol=symbol, limit=100),
            asyncio.to_thread(client.futures_exchange_info),
            asyncio.to_thread(client.futures_mark_price, symbol=symbol),
        )
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
            last_bar = runtime_status["last_processed_bar"] or "warming up"
            last_action = f"[SAR+ADX paper] {symbol} no new action; last={last_bar}"
        return _CycleResult(mark_price, last_signal, last_action, len(fills))

    def _apply_cycle(self, result: _CycleResult) -> None:
        self._sar_adx_mark_price = result.mark_price
        self.last_signal = result.last_signal
        self.last_action = result.last_action
        self.trade_count += result.fill_count

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
        self.running = False
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
