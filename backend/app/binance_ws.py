"""Merge Binance Futures trade, ticker, and mark-price WebSocket streams."""

import asyncio
import json
import urllib.parse

from loguru import logger

from .proxy import rewrite_proxy_for_runtime
from .ws_manager import manager

WS_MAINNET = "wss://fstream.binance.com"
WS_TESTNET = "wss://stream.binancefuture.com"
BROADCAST_INTERVAL_SECONDS = 0.5


class BinanceWSClient:
    def __init__(self, broadcast_interval: float = BROADCAST_INTERVAL_SECONDS):
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

    def _reset_stream_state(self):
        self._stream_cache = {}
        self._stream_event_times = {}
        self._cache_revision = 0
        self._published_revision = 0

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
        backoff = 2
        while self._running:
            try:
                await self._connect()
                backoff = 2
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.warning(
                        "Binance WS error "
                        f"({type(exc).__name__}: {exc}), retry {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _connect(self):
        import aiohttp

        symbol = self.symbol
        subscription_id = self._subscription_id
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
                await ws.send_json({
                    "method": "SUBSCRIBE",
                    "params": [
                        f"{stream}@trade",
                        f"{stream}@ticker",
                        f"{stream}@markPrice@1s",
                    ],
                    "id": 1,
                })
                logger.info(f"Binance WS connected: {symbol} combined ticker")
                async for msg in ws:
                    if not self._running or subscription_id != self._subscription_id:
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
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.warning(
                            f"Binance WS {msg.type.name}, reconnecting..."
                        )
                        return

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


binance_ws_client = BinanceWSClient()
