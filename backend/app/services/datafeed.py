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
import threading
import urllib.request
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd

_WRITE_LOCK = threading.Lock()   # 防并发写 parquet 损坏

from ..datastore import MARKET_ROOT, KLINES_DIR, PARQUET_DIR, FUNDING_DIR
from .vision_data import _opener, _norm_ms, BASE

# PARQUET_DIR is provided by datastore
PARQUET_DIR.mkdir(parents=True, exist_ok=True)
# FUNDING_DIR is provided by datastore
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
_FUNDING_LOOKBACK_DAYS = 30
_FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


class FundingDataIncompleteError(RuntimeError):
    """Raised when the funding cache cannot cover every requested month."""


def _pq_path(symbol: str, interval: str) -> Path:
    return PARQUET_DIR / f"{symbol}_{interval}.parquet"


def _to_ms(d: str) -> int:
    return int(pd.Timestamp(d, tz="UTC").timestamp() * 1000)


def _value_to_ms(value) -> int:
    if isinstance(value, (pd.Timestamp, datetime, np.datetime64)):
        return int(pd.Timestamp(value).value // 1_000_000)
    return _norm_ms(value)


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
    time_covered = (
        df["open_time"].min() <= s_ms
        and df["open_time"].max() >= eff_end - tol
    )
    if not time_covered:
        return False

    requested = df[(df["open_time"] >= s_ms) & (df["open_time"] <= eff_end)]
    if requested.empty or "taker_buy_base" not in requested.columns:
        return False
    return requested["taker_buy_base"].notna().mean() >= 0.99


def _write_pq(df: pd.DataFrame, symbol: str, interval: str) -> None:
    """精简到 _STORE 列并统一 dtype，去重排序后落盘。"""
    d = df.copy()
    for c in _STORE:
        if c not in d.columns:
            d[c] = 0
    d = d[_STORE]
    d["open_time"] = d["open_time"].apply(_value_to_ms).astype("int64")
    d["close_time"] = d["close_time"].apply(
        lambda x: _value_to_ms(x) if str(x).strip() not in ("", "nan", "None") else 0
    ).astype("int64")
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64")
    d = (
        d.dropna(subset=["open", "close"])
        .drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    d.to_parquet(_pq_path(symbol, interval), index=False)


def _cached_vision_rows(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """Load the smallest legacy JSON cache that fully covers the request."""
    s_ms, e_ms = _to_ms(start), _to_ms(end)
    prefix = f"{symbol}_{interval}_"
    candidates = []
    for path in KLINES_DIR.glob(f"{prefix}*.json"):
        suffix = path.stem[len(prefix):]
        try:
            cache_start, cache_end = suffix.split("_", maxsplit=1)
            if _to_ms(cache_start) <= s_ms and _to_ms(cache_end) >= e_ms:
                candidates.append(path)
        except (TypeError, ValueError):
            continue
    if not candidates:
        return pd.DataFrame(columns=_COLS)

    path = min(candidates, key=lambda p: p.stat().st_size)
    try:
        rows = json.loads(path.read_text())
        df = pd.DataFrame(rows, columns=_COLS)
        df["open_time"] = df["open_time"].apply(_norm_ms).astype("int64")
        return df[(df["open_time"] >= s_ms) & (df["open_time"] <= e_ms)].copy()
    except (OSError, ValueError):
        return pd.DataFrame(columns=_COLS)


def _vision_rows(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    """直接拿 vision 原始 12 列（不做 dtype 转换，便于合并）。"""
    cached = _cached_vision_rows(symbol, interval, start, end)
    if not cached.empty:
        return cached

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


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(columns=["funding_time", "rate"])


def _normalize_funding(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not {"funding_time", "rate"}.issubset(df.columns):
        return _empty_funding()
    normalized = df[["funding_time", "rate"]].copy()
    normalized["funding_time"] = normalized["funding_time"].apply(_norm_ms)
    normalized["rate"] = pd.to_numeric(normalized["rate"], errors="coerce")
    return (
        normalized.dropna(subset=["funding_time", "rate"])
        .drop_duplicates("funding_time", keep="last")
        .sort_values("funding_time")
        .reset_index(drop=True)
    )


def _read_funding_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return _empty_funding()
    try:
        return _normalize_funding(pd.read_parquet(path))
    except Exception:
        return _empty_funding()


def _next_month(month: date) -> date:
    return date(month.year + (month.month // 12), (month.month % 12) + 1, 1)


def _funding_months(start_ms: int, end_ms: int) -> list[date]:
    current = pd.Timestamp(start_ms, unit="ms", tz="UTC").date().replace(day=1)
    final = pd.Timestamp(end_ms, unit="ms", tz="UTC").date().replace(day=1)
    months = []
    while current <= final:
        months.append(current)
        current = _next_month(current)
    return months


def _funding_month_covered(
    df: pd.DataFrame, month: date, request_start_ms: int, request_end_ms: int
) -> bool:
    if df.empty:
        return False
    month_start_ms = _to_ms(month.isoformat())
    month_end_ms = _to_ms(_next_month(month).isoformat())
    segment_start = max(request_start_ms, month_start_ms)
    segment_end = min(request_end_ms, month_end_ms)
    rows = df[
        (df["funding_time"] >= month_start_ms)
        & (df["funding_time"] < month_end_ms)
    ]
    if rows.empty:
        return False
    return bool(
        rows["funding_time"].min() <= segment_start + _FUNDING_INTERVAL_MS
        and rows["funding_time"].max() >= segment_end - _FUNDING_INTERVAL_MS
    )


def _missing_funding_months(
    df: pd.DataFrame, request_start_ms: int, request_end_ms: int
) -> list[date]:
    return [
        month
        for month in _funding_months(request_start_ms, request_end_ms)
        if not _funding_month_covered(df, month, request_start_ms, request_end_ms)
    ]


def _download_funding_month(opener, symbol: str, month: date) -> pd.DataFrame:
    month_key = f"{month.year:04d}-{month.month:02d}"
    url = f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month_key}.zip"
    raw = opener.open(url, timeout=60).read()
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        with archive.open(archive.namelist()[0]) as source:
            for row in csv.reader(io.TextIOWrapper(source, "utf-8")):
                if not row or not row[0] or not row[0][0].isdigit():
                    continue
                rate = float(row[-1] if len(row) >= 3 else row[1])
                rows.append([_norm_ms(row[0]), rate])
    return _normalize_funding(pd.DataFrame(rows, columns=["funding_time", "rate"]))


def load_funding(symbol: str, start: str, end: str) -> pd.DataFrame:
    """历史资金费率（8h 一条），缓存 Parquet。列: funding_time(ms), rate(float)。"""
    fp = FUNDING_DIR / f"{symbol}.parquet"
    start_ms = _to_ms(start) - _FUNDING_LOOKBACK_DAYS * 86_400_000
    end_ms = _to_ms(end)
    with _WRITE_LOCK:
        df = _read_funding_cache(fp)
        missing = _missing_funding_months(df, start_ms, end_ms)
        errors = {}
        downloaded = []
        if missing:
            try:
                opener = _opener(_proxy())
            except Exception as exc:
                opener = None
                errors.update({month: exc for month in missing})
            if opener is not None:
                for month in missing:
                    try:
                        month_data = _download_funding_month(opener, symbol, month)
                        if not month_data.empty:
                            downloaded.append(month_data)
                    except Exception as exc:
                        errors[month] = exc

        if downloaded:
            df = _normalize_funding(pd.concat([df, *downloaded], ignore_index=True))
            fp.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(fp, index=False)

        remaining = _missing_funding_months(df, start_ms, end_ms)
        if remaining:
            missing_keys = ", ".join(f"{month.year:04d}-{month.month:02d}" for month in remaining)
            cause = next((errors[month] for month in remaining if month in errors), None)
            error = FundingDataIncompleteError(
                f"Funding cache for {symbol} is incomplete; missing months: {missing_keys}"
            )
            if cause is not None:
                raise error from cause
            raise error

    out = df[(df["funding_time"] >= start_ms) & (df["funding_time"] <= end_ms)].copy()
    return _normalize_funding(out)


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
