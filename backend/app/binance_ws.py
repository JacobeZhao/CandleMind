"""Consume Binance Futures ticker streams and completed 5m candle events."""

import asyncio
from collections.abc import Awaitable, Callable
import json
import random
import time
from typing import Any
import urllib.parse

from loguru import logger

from .proxy import rewrite_proxy_for_runtime
from .ws_manager import manager

WS_MAINNET = "wss://fstream.binance.com"
WS_TESTNET = "wss://demo-fstream.binance.com"
BROADCAST_INTERVAL_SECONDS = 0.5
KLINE_INTERVAL = "5m"
RECONNECT_BACKOFF_INITIAL_SECONDS = 2.0
RECONNECT_BACKOFF_CAP_SECONDS = 60.0
RECONNECT_STABLE_SECONDS = 30.0

ClosedKlineListener = Callable[[dict[str, Any]], Awaitable[None]]


class _UnexpectedDisconnect(ConnectionError):
    def __init__(self, *, stable: bool):
        super().__init__("Binance WebSocket disconnected unexpectedly")
        self.stable = stable


class BinanceWSClient:
    def __init__(
        self,
        broadcast_interval: float = BROADCAST_INTERVAL_SECONDS,
        *,
        closed_kline_listener: ClosedKlineListener | None = None,
    ):
        self._task: asyncio.Task | None = None
        self._publisher_task: asyncio.Task | None = None
        self._running = False
        self._subscription_id = 0
        self._ready_event = asyncio.Event()
        self._ready_subscription_id = 0
        self._incoming_context: tuple[str, int] | None = None
        self._stream_cache: dict[str, dict] = {}
        self._stream_event_times: dict[str, int] = {}
        self._cache_revision = 0
        self._published_revision = 0
        self._last_closed_kline_time: int | None = None
        self._closed_kline_listener = closed_kline_listener
        self.broadcast_interval = broadcast_interval
        self.symbol = "BTCUSDT"
        self.testnet = True
        self.proxy = None

    async def start(
        self,
        symbol: str,
        testnet: bool,
        proxy: str | None = None,
    ) -> int:
        """Start or restart the combined stream subscription."""
        await self.stop()
        self.symbol = symbol.upper()
        self.testnet = testnet
        self.proxy = proxy or None
        self._reset_stream_state()
        self._subscription_id += 1
        self._ready_subscription_id = self._subscription_id
        self._ready_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._publisher_task = asyncio.create_task(
            self._publish_loop(self._subscription_id)
        )
        logger.info(
            f"Binance WS scheduled: {self.symbol} testnet={self.testnet}"
        )
        return self._subscription_id

    async def stop(self):
        self._running = False
        self._subscription_id += 1
        self._ready_event.set()
        tasks = [task for task in (self._task, self._publisher_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._publisher_task = None
        self._ready_event = asyncio.Event()
        self._ready_subscription_id = self._subscription_id
        self._reset_stream_state()

    async def wait_until_ready(
        self,
        subscription_id: int,
        timeout: float,
    ) -> None:
        """Wait for a validated market event from one exact subscription."""
        if subscription_id != self._ready_subscription_id:
            raise RuntimeError("Binance WS subscription was replaced before readiness")
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        if subscription_id != self._subscription_id or not self._running:
            raise RuntimeError("Binance WS subscription changed while becoming ready")

    def is_ready(self, subscription_id: int | None = None) -> bool:
        expected = (
            self._subscription_id if subscription_id is None else subscription_id
        )
        return (
            self._running
            and expected == self._subscription_id
            and expected == self._ready_subscription_id
            and self._ready_event.is_set()
        )

    async def switch_symbol(self, symbol: str):
        """Reconnect to a different symbol and discard the old snapshot."""
        symbol = symbol.upper()
        if symbol == self.symbol:
            return
        if self._running:
            await self.start(symbol, self.testnet, self.proxy)
        else:
            self.symbol = symbol
            self._reset_stream_state()

    def register_closed_kline_listener(
        self, listener: ClosedKlineListener | None
    ) -> None:
        """Register the durable Harness ingress for completed 5m candles."""
        self._closed_kline_listener = listener

    def _reset_stream_state(self):
        self._stream_cache = {}
        self._stream_event_times = {}
        self._cache_revision = 0
        self._published_revision = 0
        self._last_closed_kline_time = None

    async def _publish_loop(self, subscription_id: int):
        """Publish only the newest merged snapshot at a bounded cadence."""
        while self._running and subscription_id == self._subscription_id:
            await asyncio.sleep(self.broadcast_interval)
            await self._publish_latest(subscription_id)

    async def _publish_latest(self, subscription_id: int):
        if (
            not self._running
            or subscription_id != self._subscription_id
            or self._cache_revision == self._published_revision
        ):
            return

        ticker = self._stream_cache.get("ticker")
        trade = self._stream_cache.get("trade")
        if ticker is None and trade is None:
            return

        combined = dict(ticker or {})
        combined.update(self._stream_cache.get("mark", {}))
        price_source = max(
            (source for source in (ticker, trade) if source is not None),
            key=lambda source: source["eventTime"],
        )
        combined.update({
            "symbol": price_source["symbol"],
            "price": price_source["price"],
            "eventTime": price_source["eventTime"],
        })

        # Mark this revision consumed before awaiting a potentially slow client.
        # Events received during the await advance the revision for the next tick.
        self._published_revision = self._cache_revision
        await manager.broadcast({"type": "ticker", "data": combined})

    async def _run(self):
        backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except _UnexpectedDisconnect as exc:
                if not self._running:
                    return
                if exc.stable:
                    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
                delay = self._jittered_backoff(backoff)
                logger.warning(
                    "Binance WS disconnected, retry in {:.2f}s", delay
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_SECONDS)
            except Exception as exc:
                if self._running:
                    delay = self._jittered_backoff(backoff)
                    logger.warning(
                        "Binance WS connection failed: exception_type={} "
                        "retry_seconds={:.2f}",
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_SECONDS)

    @staticmethod
    def _jittered_backoff(backoff: float) -> float:
        nominal = min(backoff, RECONNECT_BACKOFF_CAP_SECONDS)
        return min(
            random.uniform(nominal * 0.5, nominal),
            RECONNECT_BACKOFF_CAP_SECONDS,
        )

    def _mark_disconnected(self, subscription_id: int) -> None:
        if (
            self._running
            and subscription_id == self._subscription_id
            and subscription_id == self._ready_subscription_id
        ):
            self._ready_event.clear()

    async def _connect(self):
        import aiohttp

        symbol = self.symbol
        subscription_id = self._subscription_id
        self._mark_disconnected(subscription_id)
        base = WS_TESTNET if self.testnet else WS_MAINNET
        stream = symbol.lower()
        url = f"{base}/ws"
        logger.info(f"Connecting: {url}")

        connector = None
        proxy_url = None
        if self.proxy:
            runtime_proxy = rewrite_proxy_for_runtime(self.proxy)
            parsed = urllib.parse.urlparse(runtime_proxy)
            if parsed.scheme.startswith("socks"):
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(runtime_proxy)
            else:
                proxy_url = runtime_proxy

        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=True,
        ) as session:
            ws_kwargs = {"heartbeat": 20}
            if proxy_url:
                ws_kwargs["proxy"] = proxy_url

            async with session.ws_connect(url, **ws_kwargs) as ws:
                connected_at = time.monotonic()
                await ws.send_json({
                    "method": "SUBSCRIBE",
                    "params": [
                        f"{stream}@trade",
                        f"{stream}@ticker",
                        f"{stream}@markPrice@1s",
                        f"{stream}@kline_{KLINE_INTERVAL}",
                    ],
                    "id": 1,
                })
                logger.info(f"Binance WS connected: {symbol} combined ticker")
                try:
                    async for msg in ws:
                        if (
                            not self._running
                            or subscription_id != self._subscription_id
                        ):
                            return
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._incoming_context = (symbol, subscription_id)
                            try:
                                # Keep the one-argument call compatible with the
                                # readiness wrapper in routes/settings.py.
                                await self._handle(msg.data)
                            finally:
                                self._incoming_context = None
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            self._mark_disconnected(subscription_id)
                            raise _UnexpectedDisconnect(
                                stable=self._connection_was_stable(connected_at)
                            )
                except asyncio.CancelledError:
                    raise
                except _UnexpectedDisconnect:
                    raise
                except Exception as exc:
                    if (
                        not self._running
                        or subscription_id != self._subscription_id
                    ):
                        return
                    self._mark_disconnected(subscription_id)
                    raise _UnexpectedDisconnect(
                        stable=self._connection_was_stable(connected_at)
                    ) from exc

                if self._running and subscription_id == self._subscription_id:
                    self._mark_disconnected(subscription_id)
                    raise _UnexpectedDisconnect(
                        stable=self._connection_was_stable(connected_at)
                    )

    @staticmethod
    def _connection_was_stable(connected_at: float) -> bool:
        return time.monotonic() - connected_at >= RECONNECT_STABLE_SECONDS

    async def _handle(
        self,
        raw: str,
        expected_symbol: str | None = None,
        subscription_id: int | None = None,
    ):
        """Merge one stream event into the latest compatible ticker snapshot."""
        try:
            envelope = json.loads(raw)
            event = envelope.get("data", envelope)
            event_type = event.get("e")
            symbol = event.get("s")
            if self._incoming_context is not None:
                context_symbol, context_id = self._incoming_context
                expected_symbol = expected_symbol or context_symbol
                if subscription_id is None:
                    subscription_id = context_id
            expected_symbol = (expected_symbol or self.symbol).upper()

            if subscription_id is not None and subscription_id != self._subscription_id:
                return
            if not symbol or symbol.upper() != expected_symbol:
                return

            if event_type == "kline":
                await self._handle_closed_kline(
                    event,
                    expected_symbol=expected_symbol,
                    subscription_id=(
                        subscription_id
                        if subscription_id is not None
                        else self._subscription_id
                    ),
                )
                return
            if event_type == "24hrTicker":
                stream_name = "ticker"
                required = ("E", "c", "P", "h", "l", "q")
                if any(field not in event for field in required):
                    return
                snapshot = {
                    "symbol": symbol.upper(),
                    "price": event["c"],
                    "change": event["P"],
                    "high": event["h"],
                    "low": event["l"],
                    "volume": event["q"],
                    "eventTime": int(event["E"]),
                }
            elif event_type in ("aggTrade", "trade"):
                stream_name = "trade"
                if "E" not in event or "p" not in event:
                    return
                snapshot = {
                    "symbol": symbol.upper(),
                    "price": event["p"],
                    "eventTime": int(event["E"]),
                }
            elif event_type == "markPriceUpdate":
                stream_name = "mark"
                if "E" not in event or "p" not in event:
                    return
                snapshot = {"markPrice": event["p"]}
                for output_name, source_name in (
                    ("indexPrice", "i"),
                    ("fundingRate", "r"),
                    ("nextFundingTime", "T"),
                ):
                    if source_name in event:
                        snapshot[output_name] = event[source_name]
            else:
                return

            event_time = int(event.get("E", 0))
            previous_time = self._stream_event_times.get(stream_name, -1)
            if event_time and event_time < previous_time:
                return
            self._stream_event_times[stream_name] = max(event_time, previous_time)
            self._stream_cache[stream_name] = snapshot
            self._cache_revision += 1
            effective_subscription_id = (
                subscription_id
                if subscription_id is not None
                else self._subscription_id
            )
            if (
                self._running
                and effective_subscription_id == self._subscription_id
                and effective_subscription_id == self._ready_subscription_id
            ):
                self._ready_event.set()
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug(f"WS handle error: {exc}")

    async def _handle_closed_kline(
        self,
        event: dict[str, Any],
        *,
        expected_symbol: str,
        subscription_id: int,
    ) -> None:
        kline = event.get("k")
        if (
            subscription_id != self._subscription_id
            or not isinstance(kline, dict)
            or kline.get("i") != KLINE_INTERVAL
            or kline.get("x") is not True
            or str(kline.get("s", "")).upper() != expected_symbol
        ):
            return

        required_kline_fields = ("t", "T", "o", "h", "l", "c", "v", "q", "n", "V", "Q")
        if "E" not in event or any(field not in kline for field in required_kline_fields):
            return

        close_time = int(kline["T"])
        if (
            self._last_closed_kline_time is not None
            and close_time <= self._last_closed_kline_time
        ):
            return

        payload = {
            "event_type": "closed_kline",
            "source": "binance_usdm_websocket",
            "network": "testnet" if self.testnet else "mainnet",
            "symbol": expected_symbol,
            "interval": KLINE_INTERVAL,
            "subscription_epoch": subscription_id,
            "event_time": int(event["E"]),
            "open_time": int(kline["t"]),
            "close_time": close_time,
            "open": str(kline["o"]),
            "high": str(kline["h"]),
            "low": str(kline["l"]),
            "close": str(kline["c"]),
            "volume": str(kline["v"]),
            "quote_volume": str(kline["q"]),
            "trade_count": int(kline["n"]),
            "taker_buy_base_volume": str(kline["V"]),
            "taker_buy_quote_volume": str(kline["Q"]),
        }
        listener = self._closed_kline_listener
        if listener is not None:
            try:
                await listener(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Closed-kline listener failed: symbol={} close_time={} exception_type={}",
                    expected_symbol,
                    close_time,
                    type(exc).__name__,
                )
                return
        self._last_closed_kline_time = close_time

        if (
            self._running
            and subscription_id == self._ready_subscription_id
        ):
            self._ready_event.set()


binance_ws_client = BinanceWSClient()
