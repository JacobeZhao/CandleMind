"""
统一行情数据入口（Parquet）。

- 每个 (symbol, interval) 一个全量 Parquet：MARKET_ROOT/parquet/{symbol}_{interval}.parquet
- load_klines: 读 Parquet 切区间；缺失则用 vision_data 补下、合并去重、回写
- update_klines: 增量追加最新数据
- integrity_check: 缺口/重复体检
- load_funding: data.binance.vision 历史资金费率（真实费率回测用）
- manifest: 已有数据集清单
- migrate_legacy_json: 把旧的按区间切的 JSON klines 合并进 Parquet
数据源走 vision_data.fetch_vision（主网 CDN，不受 REST 限流）。
"""
import io
import csv
import json
import time
import zipfile
import calendar
import threading
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd

_WRITE_LOCK = threading.Lock()   # 防并发写 parquet 损坏

from ..datastore import MARKET_ROOT, KLINES_DIR
from .vision_data import _opener, _norm_ms, BASE

PARQUET_DIR = MARKET_ROOT / "parquet"
PARQUET_DIR.mkdir(parents=True, exist_ok=True)
FUNDING_DIR = MARKET_ROOT / "funding"
FUNDING_DIR.mkdir(parents=True, exist_ok=True)

_COLS = ["open_time", "open", "high", "low", "close", "volume",
         "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
# 落盘只存回测需要的精简列，统一 dtype（避免 JSON 混合类型导致 parquet 报错）
_STORE = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "taker_buy_base"]
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}


def _pq_path(symbol: str, interval: str) -> Path:
    return PARQUET_DIR / f"{symbol}_{interval}.parquet"


def _to_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").timestamp() * 1000)


def _read_pq(symbol: str, interval: str) -> pd.DataFrame:
    p = _pq_path(symbol, interval)
    if p.exists():
        try:
            return pd.read_parquet(p)
        except Exception:
            return pd.DataFrame(columns=_STORE)   # 损坏则当空，触发重建
    return pd.DataFrame(columns=_STORE)


def _covered(df: pd.DataFrame, s_ms: int, e_ms: int, interval: str) -> bool:
    """是否已覆盖请求区间。容忍数据滞后（vision 日更，最新缺 1-3 天属正常）。"""
    if df.empty:
        return False
    now_ms = int(time.time() * 1000)
    eff_end = min(e_ms, now_ms)
    tol = 3 * 86_400_000  # 3 天容忍
    return df["open_time"].min() <= s_ms and df["open_time"].max() >= eff_end - tol


def _write_pq(df: pd.DataFrame, symbol: str, interval: str) -> None:
    """精简到 _STORE 列并统一 dtype，去重排序后落盘。"""
    d = df.copy()
    for c in _STORE:
        if c not in d.columns:
            d[c] = 0
    d = d[_STORE]
    d["open_time"]  = d["open_time"].apply(_norm_ms).astype("int64")
    d["close_time"] = d["close_time"].apply(
        lambda x: _norm_ms(x) if str(x).strip() not in ("", "nan", "None") else 0).astype("int64")
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64")
    d = d.dropna(subset=["open", "close"]).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    d.to_parquet(_pq_path(symbol, interval), index=False)


def _vision_rows(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """直接拿 vision 原始 12 列（不做 dtype 转换，便于合并）。"""
    from .vision_data import fetch_vision
    # fetch_vision 写的是 KLINES_DIR 下的 JSON 缓存；这里复用其下载+解析，但我们要原始行
    # 简化：调用 fetch_vision 得到 DataFrame（已转 dtype），再补 open_time(ms)
    df = fetch_vision(symbol, interval, start, end, _proxy())
    if df.empty:
        return df
    out = df.copy()
    out["open_time"] = (out["open_time"].astype("int64") // 10**6)  # datetime64ns → ms
    return out[[c for c in _COLS if c in out.columns]]


def _proxy() -> str | None:
    try:
        from ..database import SessionLocal, Settings
        s = SessionLocal().query(Settings).first()
        return s.proxy_url if s else None
    except Exception:
        return None


def load_klines(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """读 Parquet 切区间；缺失区间自动补下、合并、回写。返回带 open_time(datetime) 的 df。"""
    s_ms, e_ms = _to_ms(start), _to_ms(end)
    df = _read_pq(symbol, interval)
    if not _covered(df, s_ms, e_ms, interval):
        with _WRITE_LOCK:                          # 串行化补下+写，防并发损坏
            df = _read_pq(symbol, interval)        # 锁内复检（别的线程可能已补好）
            if not _covered(df, s_ms, e_ms, interval):
                new = _vision_rows(symbol, interval, start, end)
                if not new.empty:
                    merged = new if df.empty else pd.concat([df, new], ignore_index=True)
                    _write_pq(merged, symbol, interval)
                    df = _read_pq(symbol, interval)
    sub = df[(df["open_time"] >= s_ms) & (df["open_time"] <= e_ms)].copy()
    for c in ("open", "high", "low", "close", "volume"):
        sub[c] = sub[c].astype(float)
    sub["open_time"] = pd.to_datetime(sub["open_time"], unit="ms")
    sub.reset_index(drop=True, inplace=True)
    return sub


def update_klines(symbol: str, interval: str) -> int:
    """增量：从最后一根之后下到昨天，追加。返回新增行数。"""
    df = _read_pq(symbol, interval)
    if df.empty:
        return 0
    last_ms = int(df["open_time"].max())
    start = datetime.utcfromtimestamp(last_ms / 1000).strftime("%Y-%m-%d")
    end = datetime.utcnow().strftime("%Y-%m-%d")
    before = len(df)
    load_klines(symbol, interval, start, end)
    return len(_read_pq(symbol, interval)) - before


def integrity_check(symbol: str, interval: str) -> dict:
    df = _read_pq(symbol, interval)
    if df.empty:
        return {"rows": 0, "gaps": 0, "dups": 0}
    step = _INTERVAL_MS.get(interval, 0)
    ot = df["open_time"].sort_values().reset_index(drop=True)
    dups = int(ot.duplicated().sum())
    gaps = int(((ot.diff().dropna() > step) if step else pd.Series(dtype=bool)).sum())
    return {"rows": len(df), "gaps": gaps, "dups": dups,
            "start": str(pd.to_datetime(ot.iloc[0], unit="ms")),
            "end": str(pd.to_datetime(ot.iloc[-1], unit="ms"))}


def load_funding(symbol: str, start: str, end: str) -> pd.DataFrame:
    """历史资金费率（8h 一条），缓存 Parquet。列: funding_time(ms), rate(float)。"""
    fp = FUNDING_DIR / f"{symbol}.parquet"
    if fp.exists():
        df = pd.read_parquet(fp)
    else:
        op = _opener(_proxy())
        sdt = datetime.strptime(start, "%Y-%m-%d").date()
        edt = datetime.strptime(end, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        rows = []
        cur = sdt.replace(day=1)
        while cur <= edt:
            y, m = cur.year, cur.month
            last = date(y, m, calendar.monthrange(y, m)[1])
            url = f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{y:04d}-{m:02d}.zip"
            try:
                raw = op.open(url, timeout=60).read()
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    with z.open(z.namelist()[0]) as f:
                        for r in csv.reader(io.TextIOWrapper(f, "utf-8")):
                            if not r or not r[0] or not r[0][0].isdigit():
                                continue
                            rows.append([_norm_ms(r[0]), float(r[-1] if len(r) >= 3 else r[1])])
            except Exception:
                pass
            cur = date(y + (m // 12), (m % 12) + 1, 1)
        df = pd.DataFrame(rows, columns=["funding_time", "rate"]).drop_duplicates("funding_time")
        if not df.empty:
            df.to_parquet(fp, index=False)
    return df


def data_signature(symbols, intervals=("1d", "1h", "15m")) -> str:
    """数据快照指纹（文件大小+修改时间哈希），用于回测可复现性。"""
    import hashlib
    h = hashlib.sha1()
    for s in symbols:
        for itv in intervals:
            p = _pq_path(s, itv)
            if p.exists():
                st = p.stat()
                h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:12]


def manifest() -> list:
    out = []
    for p in sorted(PARQUET_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p, columns=["open_time"])
            out.append({"file": p.stem, "rows": len(df),
                        "start": str(pd.to_datetime(df["open_time"].min(), unit="ms")),
                        "end": str(pd.to_datetime(df["open_time"].max(), unit="ms"))})
        except Exception:
            pass
    return out


def migrate_legacy_json() -> dict:
    """把 KLINES_DIR 下旧的 {symbol}_{interval}_{start}_{end}.json 合并进 Parquet。"""
    merged = {}
    for jf in KLINES_DIR.glob("*.json"):
        parts = jf.stem.split("_")
        if len(parts) < 4:
            continue
        symbol, interval = parts[0], parts[1]
        try:
            rows = json.loads(jf.read_text())
        except Exception:
            continue
        merged.setdefault((symbol, interval), []).extend(rows)
    count = {}
    for (symbol, interval), rows in merged.items():
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=_COLS)
        existing = _read_pq(symbol, interval)
        if not existing.empty:
            df = pd.concat([existing, df], ignore_index=True)   # _write_pq 负责精简+类型
        _write_pq(df, symbol, interval)
        count[f"{symbol}_{interval}"] = len(_read_pq(symbol, interval))
    return count
