"""
币种特征空间：给每个币算一组（尽量稳定的）特征，用于特征自适应参数。
只用 asof 之前的数据（point-in-time，无前视）。特征库存 G 盘。

稳定性实测(2025 vs 2026 跨期相关)：vol +0.97 / autocorr +0.91 可用；
variance_ratio +0.41 中等；ER +0.08 不稳定（勿用作选币）。
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .datafeed import load_klines
from .indicators import compute_many
from ..datastore import MARKET_ROOT

FEAT_DIR = MARKET_ROOT / "features"
FEAT_DIR.mkdir(parents=True, exist_ok=True)

# 跨期稳定（可放心用于自适应）的特征
STABLE = {"vol", "atr_pct", "autocorr_1", "variance_ratio", "hurst", "btc_corr", "beta_btc"}


def _hurst(logp: np.ndarray) -> float:
    """差分方差法估 Hurst：H>0.5 趋势/持续，H<0.5 均值回归。"""
    if len(logp) < 40:
        return 0.5
    lags = range(2, 20)
    tau = [np.std(logp[lag:] - logp[:-lag]) or 1e-9 for lag in lags]
    return float(np.polyfit(np.log(list(lags)), np.log(tau), 1)[0])


def compute_features(symbol: str, start: str, end: str, btc_ret: pd.Series = None) -> dict:
    """对 [start,end] 的日线算特征。btc_ret 传入则算相关/beta。"""
    df = load_klines(symbol, "1d", start, end)
    if len(df) < 40:
        return {"symbol": symbol, "n": len(df), "ok": False}
    c = df["close"].reset_index(drop=True)
    r = c.pct_change().dropna()
    logp = np.log(c.to_numpy())
    # 趋势效率 ER（实测不稳，仅作参考标记）
    er = []
    for i in range(10, len(c)):
        seg = c[i - 10:i + 1]; net = abs(seg.iloc[-1] - seg.iloc[0]); v = seg.diff().abs().sum()
        er.append(net / v if v > 0 else 0)
    r5 = c.pct_change(5).dropna()
    vr = float(r5.var() / (5 * r.var())) if r.var() > 0 else 1.0
    adx = compute_many(df, [{"id": "adx", "params": {"period": 14}}])["adx14"].dropna()
    feat = {
        "symbol": symbol, "n": len(df), "ok": True,
        "vol": round(float(r.std()) * 100, 3),                    # 日收益率波动率 %
        "atr_pct": round(float((df["high"] - df["low"]).mean() / c.mean()) * 100, 3),
        "autocorr_1": round(float(r.autocorr(1) or 0), 3),        # 动量(+)/回归(-)
        "variance_ratio": round(vr, 3),                            # >1趋势 <1回归
        "hurst": round(_hurst(logp), 3),                           # >0.5趋势 <0.5回归
        "er_mean": round(float(np.mean(er)) if er else 0, 3),     # 不稳定，标记勿用
        "adx_mean": round(float(adx.mean()) if len(adx) else 0, 2),
        "dollar_vol": round(float(df["quote_volume"].astype(float).mean()), 0),  # 流动性
    }
    if btc_ret is not None:
        j = pd.concat([r.reset_index(drop=True), btc_ret.reset_index(drop=True)], axis=1).dropna()
        if len(j) > 20 and j.iloc[:, 1].var() > 0:
            feat["btc_corr"] = round(float(j.iloc[:, 0].corr(j.iloc[:, 1])), 3)
            feat["beta_btc"] = round(float(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var()), 3)
    return feat


def build_feature_store(symbols, end: str, lookback_days: int = 180, save: bool = True) -> pd.DataFrame:
    """对一组币算 [end-lookback, end] 的特征，组装+存盘。"""
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    btc = load_klines("BTCUSDT", "1d", start, end)
    btc_ret = btc["close"].pct_change().dropna() if len(btc) else None
    rows = [compute_features(s, start, end, btc_ret) for s in symbols]
    rows = [r for r in rows if r.get("ok")]
    fdf = pd.DataFrame(rows)
    if save and not fdf.empty:
        fdf.to_parquet(FEAT_DIR / f"{end}.parquet", index=False)
        fdf.to_parquet(FEAT_DIR / "latest.parquet", index=False)
    return fdf


def load_latest() -> pd.DataFrame:
    p = FEAT_DIR / "latest.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def stability_check(symbols, mid="2026-01-01", end="2026-06-05", lookback_days=365) -> dict:
    """跨期(前段 vs 后段)特征相关性，判断哪些特征稳定可用。"""
    a = build_feature_store(symbols, mid, lookback_days, save=False).set_index("symbol")
    b = build_feature_store(symbols, end, 150, save=False).set_index("symbol")
    common = a.index.intersection(b.index)
    out = {}
    for col in ("vol", "atr_pct", "autocorr_1", "variance_ratio", "hurst", "er_mean", "adx_mean"):
        if col in a and col in b:
            x, y = a.loc[common, col], b.loc[common, col]
            out[col] = round(float(np.corrcoef(x, y)[0, 1]), 2) if len(common) > 2 and x.std() and y.std() else None
    return out
