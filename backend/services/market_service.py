import os
import asyncio
import json
import time
from datetime import datetime, timedelta, time as dtime
from collections import deque
from typing import Dict
from pathlib import Path
from typing import List

import requests
import websockets


# ================== 缓存单元 ==================

class KlineCache:
    def __init__(self):
        self.klines = deque(maxlen=int(os.environ.get("MAX_LEN", 1000)))
        self.current = None

    def load(self, klines: list[dict]):
        for k in klines[-os.environ.get("MAX_LEN", 1000):]:
            self.klines.append(k)

    def append_closed(self, kline: dict):
        self.klines.append(kline)

    def update_current(self, kline: dict):
        self.current = kline

    def snapshot(self):
        return list(self.klines)


class KlineStorage:
    def __init__(self, data_dir: str = "datas/kline_data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

    def _path(self, symbol: str, interval: str) -> Path:
        return self.data_dir / f"{symbol}_{interval}.json"

    def load(self, symbol: str, interval: str) -> List[dict]:
        path = self._path(symbol, interval)
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, symbol: str, interval: str, klines: List[dict]):
        path = self._path(symbol, interval)
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                klines,
                f,
                ensure_ascii=False,
                default=str,
            )


# ================== 行情服务 ==================

class MarketDataService:
    def __init__(self):
        self.storage = KlineStorage()
        self.data: Dict[str, Dict[str, KlineCache]] = {
            s: {i: KlineCache() for i in os.environ.get("INTERVALS", "1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M")}
            for s in os.environ.get("SYMBOLS")
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

    async def stop(self):
        print("🛑 行情服务停止")
        self._running = False
        await asyncio.sleep(1)

    # ---------- 历史数据 ----------

    def _load_or_fetch_all(self):
        for s in os.environ.get("SYMBOLS"):
            for i in os.environ.get("INTERVALS"):
                success = False
                attempts = 0
                while not success and attempts < 3:
                    try:
                        print(f"⬇️ 拉取 {s.upper()} {i} 历史数据")
                        klines = fetch_history(s, i, os.environ.get("MAX_LEN", 1000))
                        self.storage.save_klines(s, i, klines)
                        self.data[s][i].load(klines)
                        success = True
                    except Exception as e:
                        attempts += 1
                        print(f"❌ {s} {i} 拉取失败 ({attempts}/3): {e}")
                        if attempts < 3:
                            time.sleep(2)
                        else:
                            print(f"⚠️ {s} {i} 拉取失败超过 3 次，跳过")

    # ---------- WebSocket ----------

    async def _run_ws(self):
        streams = [
            f"{s}@kline_{i}"
            for s in os.environ.get("SYMBOLS")
            for i in os.environ.get("INTERVALS")
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
                        data = json.loads(msg)["datas"]
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
            tomorrow = datetime.combine(now.date(), dtime.min) + timedelta(days=1)
            await asyncio.sleep((tomorrow - now).seconds)

            print("🧹 凌晨刷新历史数据")
            for s in os.environ.get("SYMBOLS"):
                for i in os.environ.get("INTERVALS"):
                    print(f"⬇️ 拉取 {s.upper()} {i} 历史数据")
                    klines = fetch_history(s, i, limit=1000)  # fetch_history 已有重试
                    self.storage.save_klines(s, i, klines)
                    self.data[s][i].klines.clear()
                    self.data[s][i].load(klines)
                    await asyncio.sleep(0.2)  # 异步延迟


# ================== Binance REST ==================

def fetch_history(symbol: str, interval: str, limit: int, max_retries=3, retry_delay=2):
    url = "https://api.binance.com/api/v3/klines"
    result = []
    end_time = None
    retries = 0

    while len(result) < limit:
        try:
            params = {"symbol": symbol.upper(), "interval": interval, "limit": 1000}
            if end_time:
                params["endTime"] = end_time

            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
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
            retries = 0  # 成功一次就重置重试计数

        except requests.exceptions.RequestException as e:
            retries += 1
            if retries > max_retries:
                print(f"⚠️ {symbol} {interval} 拉取失败超过 {max_retries} 次，跳过")
                break
            print(f"❌ {symbol} {interval} 拉取失败 ({retries}/{max_retries})：{e}，等待 {retry_delay}s 重试...")
            time.sleep(retry_delay)

    return result[-limit:]
