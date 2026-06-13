"""
从 data.binance.vision 批量下载历史 K 线（主网真实数据，走 CDN，不受 REST -1003 限流）。

把结果写成和 services/backtest.fetch_historical 完全一致的缓存格式
（data/klines/{symbol}_{interval}_{start}_{end}.json，元素为 12 列数组），
这样回测器 run_mtf_backtest 读缓存时透明可用。

futures USDⓈ-M 路径：
  monthly: /data/futures/um/monthly/klines/{SYM}/{int}/{SYM}-{int}-{YYYY-MM}.zip
  daily:   /data/futures/um/daily/klines/{SYM}/{int}/{SYM}-{int}-{YYYY-MM-DD}.zip
"""
import io
import csv
import json
import zipfile
import calendar
import urllib.request
from datetime import datetime, date, timedelta

import pandas as pd

from .backtest import cache_path

BASE = "https://data.binance.vision/data/futures/um"


def _opener(proxy: str | None):
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _dl_csv(url: str, op, allow_404=False) -> list:
    """下载一个 zip，解压取出唯一 CSV，返回行列表（已跳过表头）。"""
    try:
        raw = op.open(url, timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and allow_404:
            return []
        raise
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            for r in csv.reader(io.TextIOWrapper(f, "utf-8")):
                if not r or not r[0] or not (r[0][0].isdigit()):   # 跳过可能的表头
                    continue
                rows.append(r)
    return rows


def _norm_ms(v) -> int:
    """open_time 归一到毫秒（vision 部分数据为微秒）。"""
    x = int(float(v))
    return x // 1000 if x > 1e14 else x


def fetch_vision(symbol: str, interval: str, start: str, end: str,
                 proxy: str = None, batch_delay: float = 0.0) -> pd.DataFrame:
    cp = cache_path(symbol, interval, start, end)
    if cp.exists():
        raw = json.loads(cp.read_text())
    else:
        op = _opener(proxy)
        sdt = datetime.strptime(start, "%Y-%m-%d").date()
        edt = datetime.strptime(end, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        rows = []
        cur = sdt.replace(day=1)
        while cur <= edt:
            y, m = cur.year, cur.month
            last = date(y, m, calendar.monthrange(y, m)[1])
            if last < today:   # 完整月 → 月度包
                url = f"{BASE}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{y:04d}-{m:02d}.zip"
                rows += _dl_csv(url, op, allow_404=True)
            else:              # 当月 → 逐日包（今天的还没生成）
                d = max(cur, sdt)
                while d <= min(last, edt, today - timedelta(days=1)):
                    url = f"{BASE}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d.isoformat()}.zip"
                    rows += _dl_csv(url, op, allow_404=True)
                    d += timedelta(days=1)
            cur = date(y + (m // 12), (m % 12) + 1, 1)
        # 规整 + 过滤区间 + 排序，写成 12 列数组（与 fetch_historical 缓存一致）
        s_ms = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
        e_ms = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)
        out = []
        for r in rows:
            ot = _norm_ms(r[0])
            if s_ms <= ot <= e_ms:
                ct = _norm_ms(r[6]) if len(r) > 6 else ot
                out.append([ot, r[1], r[2], r[3], r[4], r[5], ct,
                            r[7] if len(r) > 7 else "0", r[8] if len(r) > 8 else "0",
                            r[9] if len(r) > 9 else "0", r[10] if len(r) > 10 else "0", "0"])
        out.sort(key=lambda x: x[0])
        cp.write_text(json.dumps(out))
        raw = out

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.reset_index(drop=True, inplace=True)
    return df
