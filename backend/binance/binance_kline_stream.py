import asyncio
import json
import websockets
from datetime import datetime
from collections import deque
import math


class BinanceKlineStream:
    def __init__(self, symbol: str, interval: str, max_len: int = 100_000):
        self.symbol = symbol.lower()
        self.interval = interval
        self.max_len = max_len

        self.ws_url = (
            f"wss://stream.binance.com:9443/ws/"
            f"{self.symbol}@kline_{self.interval}"
        )

        self.klines = deque(maxlen=max_len)
        self.current_kline = None

        self._task = asyncio.create_task(self._run())

    # ================== 公共接口 ==================

    def get_klines(self):
        """获取所有已收盘 K 线"""
        return list(self.klines)

    def get_latest_kline(self):
        """获取最新一根 K 线（可能未收盘）"""
        return self.current_kline

    # ================== WebSocket 主循环 ==================

    async def _run(self):
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    print(f"✅ {self.symbol.upper()} {self.interval} WebSocket 已连接")

                    async for msg in ws:
                        self._handle_message(msg)

            except Exception as e:
                print(f"❌ WebSocket 错误: {e}，5 秒后重连")
                await asyncio.sleep(5)

    def _handle_message(self, msg: str):
        data = json.loads(msg)
        k = data["k"]

        kline = {
            "open_time": datetime.fromtimestamp(k["t"] / 1000),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }

        if not k["x"]:
            self.current_kline = kline
            return

        # K 线收盘
        self._append_kline(kline)
        self.current_kline = None

    # ================== K 线 & 指标处理 ==================

    def _append_kline(self, kline: dict):
        closes = [k["close"] for k in self.klines] + [kline["close"]]

        kline["ma"] = {
            5: self._ma(closes, 5),
            20: self._ma(closes, 20),
        }

        kline["ema"] = {
            12: self._ema(closes, 12),
            26: self._ema(closes, 26),
        }

        kline["macd"] = self._macd(closes)
        kline["rsi"] = self._rsi(closes, 14)
        kline["boll"] = self._bollinger(closes, 20)

        self.klines.append(kline)

    # ================== 私有：技术指标 ==================

    def _ma(self, data, period):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    def _ema(self, data, period):
        if len(data) < period:
            return None
        k = 2 / (period + 1)
        ema = data[-period]
        for price in data[-period + 1:]:
            ema = price * k + ema * (1 - k)
        return ema

    def _macd(self, data):
        ema12 = self._ema(data, 12)
        ema26 = self._ema(data, 26)
        if ema12 is None or ema26 is None:
            return None

        dif = ema12 - ema26
        dea = self._ema([dif], 9)
        macd = (dif - dea) * 2 if dea else None

        return {
            "dif": dif,
            "dea": dea,
            "macd": macd,
        }

    def _rsi(self, data, period):
        if len(data) < period + 1:
            return None

        gains, losses = 0, 0
        for i in range(-period, 0):
            diff = data[i] - data[i - 1]
            if diff >= 0:
                gains += diff
            else:
                losses -= diff

        if losses == 0:
            return 100

        rs = gains / losses
        return 100 - (100 / (1 + rs))

    def _bollinger(self, data, period):
        if len(data) < period:
            return None

        ma = self._ma(data, period)
        variance = sum((x - ma) ** 2 for x in data[-period:]) / period
        std = math.sqrt(variance)

        return {
            "upper": ma + 2 * std,
            "middle": ma,
            "lower": ma - 2 * std,
        }
