"""
Binance Futures WebSocket 行情订阅。
订阅 <symbol>@ticker 流，实时推送到前端，替代 REST 轮询，彻底避免限流。
支持 HTTP 代理和 SOCKS5 代理（Clash 两种模式均可）。
"""
import asyncio
import json
import urllib.parse
from loguru import logger
from .ws_manager import manager

WS_MAINNET = "wss://fstream.binance.com/ws"
WS_TESTNET = "wss://stream.binancefuture.com/ws"


class BinanceWSClient:
    def __init__(self):
        self._task:   asyncio.Task | None = None
        self._running = False
        self.symbol   = "BTCUSDT"
        self.testnet  = True
        self.proxy    = None

    # ── 外部接口 ──────────────────────────────────────────────────────────────

    async def start(self, symbol: str, testnet: bool, proxy: str = None):
        """启动（或重启）WS 订阅。"""
        await self.stop()
        self.symbol   = symbol.upper()
        self.testnet  = testnet
        self.proxy    = proxy or None
        self._running = True
        self._task    = asyncio.create_task(self._run())
        logger.info(f"Binance WS scheduled: {self.symbol} testnet={testnet}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def switch_symbol(self, symbol: str):
        """切换品种，自动重连。"""
        if symbol.upper() == self.symbol:
            return
        self.symbol = symbol.upper()
        if self._running:
            await self.start(self.symbol, self.testnet, self.proxy)

    # ── 内部逻辑 ──────────────────────────────────────────────────────────────

    async def _run(self):
        backoff = 2
        while self._running:
            try:
                await self._connect()
                backoff = 2
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"Binance WS error ({type(e).__name__}: {e}), retry {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _connect(self):
        import aiohttp

        base = WS_TESTNET if self.testnet else WS_MAINNET
        url  = f"{base}/{self.symbol.lower()}@ticker"
        logger.info(f"Connecting: {url}")

        connector = None
        proxy_url = None

        if self.proxy:
            parsed = urllib.parse.urlparse(self.proxy)
            if parsed.scheme.startswith("socks"):
                # SOCKS5/SOCKS4 代理
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self.proxy)
            else:
                # HTTP/HTTPS 代理
                proxy_url = self.proxy

        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=True,
        ) as session:
            ws_kw = {"heartbeat": 20}
            if proxy_url:
                ws_kw["proxy"] = proxy_url

            async with session.ws_connect(url, **ws_kw) as ws:
                logger.info(f"Binance WS connected: {self.symbol}@ticker")
                async for msg in ws:
                    if not self._running:
                        return
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        logger.warning(f"Binance WS {msg.type.name}, reconnecting...")
                        return

    async def _handle(self, raw: str):
        try:
            d = json.loads(raw)
            if d.get("e") != "24hrTicker":
                return
            await manager.broadcast({
                "type": "ticker",
                "data": {
                    "symbol":  d["s"],
                    "price":   d["c"],   # 最新成交价
                    "change":  d["P"],   # 24h 涨跌幅 %
                    "high":    d["h"],
                    "low":     d["l"],
                    "volume":  d["q"],   # 成交额 USDT
                },
            })
        except Exception as e:
            logger.debug(f"WS handle error: {e}")


binance_ws_client = BinanceWSClient()
