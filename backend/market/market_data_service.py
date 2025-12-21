import asyncio
import json
import time
from datetime import datetime, timedelta, time as dtime
from collections import deque
from typing import Dict

import requests
import websockets

from .storage import load_klines, save_klines

# ================== 配置 ==================

SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

INTERVALS = [
    "1m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "12h",
    "1d", "1w", "1M",
]

MAX_LEN = 1000


# ================== 缓存单元 ==================

class KlineCache:
    def __init__(self):
        self.klines = deque(maxlen=MAX_LEN)
        self.current = None

    def load(self, klines: list[dict]):
        for k in klines[-MAX_LEN:]:
            self.klines.append(k)

    def append_closed(self, kline: dict):
        self.klines.append(kline)

    def update_current(self, kline: dict):
        self.current = kline

    def snapshot(self):
        return list(self.klines)


# ================== 行情服务 ==================

class MarketDataService:
    def __init__(self):
        self.data: Dict[str, Dict[str, KlineCache]] = {
            s: {i: KlineCache() for i in INTERVALS}
            for s in SYMBOLS
        }
        self.ready = False
        self._running = False

    # ---------- 启动 ----------

    async def start(self):
        print("🚀 行情服务启动")

        print("📦 加载 / 拉取历史 K 线...")
        self._load_or_fetch_all()

        print("📡 启动 WebSocket 实时行情...")
        self._running = True
        asyncio.create_task(self._run_ws())
        asyncio.create_task(self._daily_refresh())

        self.ready = True
        print("✅ 行情服务已就绪")

    # ---------- 历史数据 ----------

    def _load_or_fetch_all(self):
        for s in SYMBOLS:
            for i in INTERVALS:
                # 尝试加载
                # klines = load_klines(s, i)
                # if not klines:
                #     print(f"⬇️ 拉取 {s.upper()} {i} 历史数据")
                #     klines = fetch_history(s, i, MAX_LEN)
                #     save_klines(s, i, klines)
                # 直接加载
                print(f"⬇️ 拉取 {s.upper()} {i} 历史数据")
                klines = fetch_history(s, i, MAX_LEN)
                save_klines(s, i, klines)
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    
                self.data[s][i].load(klines)

    # ---------- WebSocket ----------

    async def _run_ws(self):
        streams = [
            f"{s}@kline_{i}"
            for s in SYMBOLS
            for i in INTERVALS
        ]

        url = (
                "wss://stream.binance.com:9443/stream?streams="
                + "/".join(streams)
        )

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    print("✅ WebSocket 已连接")

                    async for msg in ws:
                        data = json.loads(msg)["data"]
                        self._handle_ws(data)

            except Exception as e:
                print("❌ WebSocket 错误:", e)
                await asyncio.sleep(5)

    def _handle_ws(self, data: dict):
        k = data["k"]
        s = k["s"].lower()
        i = k["i"]

        cache = self.data[s][i]
        kline = {
            "open_time": datetime.fromtimestamp(k["t"] / 1000),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "is_closed": k["x"],
        }

        if k["x"]:
            cache.append_closed(kline)
        else:
            cache.update_current(kline)

    # ---------- 每日凌晨刷新 ----------

    async def _daily_refresh(self):
        while True:
            now = datetime.now()
            tomorrow = datetime.combine(
                now.date(), dtime.min
            ) + timedelta(days=1)

            await asyncio.sleep((tomorrow - now).seconds)

            print("🧹 凌晨刷新历史数据")
            for s in SYMBOLS:
                for i in INTERVALS:
                    klines = fetch_history(s, i, MAX_LEN)
                    save_klines(s, i, klines)

                    self.data[s][i].klines.clear()
                    self.data[s][i].load(klines)


# ================== Binance REST ==================

def fetch_history(symbol: str, interval: str, limit: int):
    url = "https://api.binance.com/api/v3/klines"
    result = []
    end_time = None

    while len(result) < limit:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": 1000,
        }
        if end_time:
            params["endTime"] = end_time

        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if not data:
            break

        for k in data:
            result.append({
                "open_time": datetime.fromtimestamp(k[0] / 1000),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "is_closed": True,
            })

        end_time = data[0][0] - 1
        time.sleep(0.05)

    return result[-limit:]
