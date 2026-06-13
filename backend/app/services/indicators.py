"""
Unified indicator library. All indicators are pure functions on pd.DataFrame.
REGISTRY maps indicator_id → {name, category, panel, params, fn, outputs}
"""
import numpy as np
import pandas as pd
from typing import Dict, Any


# ── Primitive helpers ─────────────────────────────────────────────────────────

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)

def _rma(s: pd.Series, n: int) -> pd.Series:  # Wilder's smoothed MA
    return s.ewm(com=n - 1, adjust=False).mean()

def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


# ── Trend ─────────────────────────────────────────────────────────────────────

def _calc_sma(df, p):
    n = p.get("period", 20)
    return {f"sma{n}": _sma(df["close"], n)}

def _calc_ema(df, p):
    n = p.get("period", 20)
    return {f"ema{n}": _ema(df["close"], n)}

def _calc_wma(df, p):
    n = p.get("period", 20)
    return {f"wma{n}": _wma(df["close"], n)}

def _calc_dema(df, p):
    n = p.get("period", 20)
    e1 = _ema(df["close"], n)
    return {f"dema{n}": 2 * e1 - _ema(e1, n)}

def _calc_supertrend(df, p):
    atr_period = p.get("atr_period", 10)
    mult       = p.get("multiplier", 3.0)
    atr  = _atr(df, atr_period)
    hl2  = (df["high"] + df["low"]) / 2
    ub   = (hl2 + mult * atr).values
    lb   = (hl2 - mult * atr).values
    c    = df["close"].values
    n    = len(df)
    upper, lower = ub.copy(), lb.copy()
    direction = np.ones(n, dtype=np.int8)
    st = np.full(n, np.nan)
    for i in range(1, n):
        upper[i] = ub[i] if ub[i] < upper[i-1] or c[i-1] > upper[i-1] else upper[i-1]
        lower[i] = lb[i] if lb[i] > lower[i-1] or c[i-1] < lower[i-1] else lower[i-1]
        direction[i] = 1 if c[i] > upper[i] else (-1 if c[i] < lower[i] else direction[i-1])
        st[i] = lower[i] if direction[i] == 1 else upper[i]
    return {
        "supertrend":     pd.Series(st, index=df.index),
        "supertrend_dir": pd.Series(direction.astype(int), index=df.index),
    }

def _calc_psar(df, p):
    step    = p.get("step", 0.02)
    max_af  = p.get("max", 0.2)
    high, low = df["high"].values, df["low"].values
    n = len(df)
    psar = np.full(n, np.nan)
    psar[0] = low[0]
    bull = True
    af   = step
    ep   = high[0]
    for i in range(1, n):
        prev = psar[i - 1]
        if bull:
            psar[i] = prev + af * (ep - prev)
            psar[i] = min(psar[i], low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < psar[i]:
                bull, psar[i], ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep = high[i]
                af = min(af + step, max_af)
        else:
            psar[i] = prev + af * (ep - prev)
            psar[i] = max(psar[i], high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > psar[i]:
                bull, psar[i], ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep = low[i]
                af = min(af + step, max_af)
    return {"psar": pd.Series(psar, index=df.index)}

def _calc_ichimoku(df, p):
    tk = p.get("tenkan", 9)
    kj = p.get("kijun", 26)
    sb = p.get("senkou_b", 52)
    def mid(n): return (df["high"].rolling(n).max() + df["low"].rolling(n).min()) / 2
    tenkan   = mid(tk)
    kijun    = mid(kj)
    senkou_a = ((tenkan + kijun) / 2).shift(kj)
    senkou_b = mid(sb).shift(kj)
    chikou   = df["close"].shift(-kj)
    return {
        "ichi_tenkan":   tenkan,
        "ichi_kijun":    kijun,
        "ichi_senkou_a": senkou_a,
        "ichi_senkou_b": senkou_b,
        "ichi_chikou":   chikou,
    }


# ── Volatility ────────────────────────────────────────────────────────────────

def _calc_bb(df, p):
    n   = p.get("period", 20)
    std = p.get("std_dev", 2.0)
    mid = _sma(df["close"], n)
    s   = df["close"].rolling(n).std(ddof=0)
    return {"bb_upper": mid + std * s, "bb_middle": mid, "bb_lower": mid - std * s}

def _calc_keltner(df, p):
    n    = p.get("period", 20)
    mult = p.get("multiplier", 2.0)
    mid  = _ema(df["close"], n)
    atr  = _atr(df, n)
    return {"kc_upper": mid + mult * atr, "kc_middle": mid, "kc_lower": mid - mult * atr}

def _calc_donchian(df, p):
    n = p.get("period", 20)
    hi = df["high"].rolling(n).max()
    lo = df["low"].rolling(n).min()
    return {"dc_upper": hi, "dc_middle": (hi + lo) / 2, "dc_lower": lo}

def _calc_atr(df, p):
    n = p.get("period", 14)
    return {f"atr{n}": _atr(df, n)}


# ── Volume ────────────────────────────────────────────────────────────────────

def _calc_vwap(df, p):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    cv  = df["volume"].cumsum()
    ctv = (tp * df["volume"]).cumsum()
    return {"vwap": ctv / cv.replace(0, np.nan)}

def _calc_obv(df, p):
    d = np.sign(df["close"].diff().fillna(0))
    return {"obv": (d * df["volume"]).cumsum()}

def _calc_mfi(df, p):
    n   = p.get("period", 14)
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    mf  = tp * df["volume"]
    diff = tp.diff()
    pos = mf.where(diff > 0, 0).rolling(n).sum()
    neg = mf.where(diff < 0, 0).rolling(n).sum().abs()
    return {f"mfi{n}": 100 - 100 / (1 + pos / neg.replace(0, np.nan))}


# ── Oscillators ───────────────────────────────────────────────────────────────

def _calc_rsi(df, p):
    n  = p.get("period", 14)
    d  = df["close"].diff()
    ag = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    al = (-d.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    return {f"rsi{n}": 100 - (100 / (1 + ag / al.replace(0, np.nan)))}

def _calc_stoch(df, p):
    k_p  = p.get("k", 14)
    d_p  = p.get("d", 3)
    sm   = p.get("smooth", 3)
    ll   = df["low"].rolling(k_p).min()
    hh   = df["high"].rolling(k_p).max()
    k    = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    k    = k.rolling(sm).mean()
    return {"stoch_k": k, "stoch_d": k.rolling(d_p).mean()}

def _calc_cci(df, p):
    n  = p.get("period", 20)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sm = _sma(tp, n)
    md = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return {f"cci{n}": (tp - sm) / (0.015 * md.replace(0, np.nan))}

def _calc_williams_r(df, p):
    n  = p.get("period", 14)
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return {f"willr{n}": -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)}


# ── Momentum ──────────────────────────────────────────────────────────────────

def _calc_macd(df, p):
    fast   = p.get("fast", 12)
    slow   = p.get("slow", 26)
    signal = p.get("signal", 9)
    line   = _ema(df["close"], fast) - _ema(df["close"], slow)
    sig    = _ema(line, signal)
    return {"macd_line": line, "macd_signal": sig, "macd_hist": line - sig}

def _calc_adx(df, p):
    n     = p.get("period", 14)
    h, l  = df["high"], df["low"]
    pc    = df["close"].shift(1)
    tr    = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    pdm   = (h - h.shift(1)).clip(lower=0)
    ndm   = (l.shift(1) - l).clip(lower=0)
    pdm   = pdm.where(pdm > (l.shift(1) - l).clip(lower=0), 0)
    ndm   = ndm.where(ndm > (h - h.shift(1)).clip(lower=0), 0)
    atr14 = _rma(tr, n)
    pdi   = 100 * _rma(pdm, n) / atr14.replace(0, np.nan)
    ndi   = 100 * _rma(ndm, n) / atr14.replace(0, np.nan)
    dx    = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return {f"adx{n}": _rma(dx, n), "pdi": pdi, "ndi": ndi}

def _calc_roc(df, p):
    n = p.get("period", 12)
    return {f"roc{n}": df["close"].pct_change(n) * 100}


# ── Compound ──────────────────────────────────────────────────────────────────

def _calc_squeeze(df, p):
    bb_p   = p.get("bb_period", 20)
    kc_p   = p.get("kc_period", 20)
    bb_std = p.get("bb_std", 2.0)
    kc_m   = p.get("kc_mult", 1.5)
    bb     = _calc_bb(df, {"period": bb_p, "std_dev": bb_std})
    kc     = _calc_keltner(df, {"period": kc_p, "multiplier": kc_m})
    # Momentum: linreg of (close - midpoint of high/low channel)
    mid    = (df["high"].rolling(kc_p).max() + df["low"].rolling(kc_p).min()) / 2
    delta  = df["close"] - (mid + _sma(df["close"], kc_p)) / 2

    def linreg(x):
        idx = np.arange(len(x))
        m, b = np.polyfit(idx, x, 1) if len(x) > 1 else (0, x[-1])
        return m * (len(x) - 1) + b

    momentum = delta.rolling(kc_p).apply(linreg, raw=True)
    squeeze  = (bb["bb_upper"] < kc["kc_upper"]).astype(float)
    return {"sqz_momentum": momentum, "sqz_on": squeeze}

def _calc_bb_pctb(df, p):
    bb  = _calc_bb(df, p)
    bw  = (bb["bb_upper"] - bb["bb_lower"]).replace(0, np.nan)
    return {"bb_pctb": (df["close"] - bb["bb_lower"]) / bw * 100}


# ── Registry ──────────────────────────────────────────────────────────────────

REGISTRY: Dict[str, Dict[str, Any]] = {
    # id: name, category, panel, default_params, fn, outputs
    "sma":        {"name": "SMA",             "category": "trend",      "panel": "main",
                   "params": {"period": 20},
                   "outputs": ["sma{period}"],              "fn": _calc_sma},
    "ema":        {"name": "EMA",             "category": "trend",      "panel": "main",
                   "params": {"period": 20},
                   "outputs": ["ema{period}"],              "fn": _calc_ema},
    "wma":        {"name": "WMA",             "category": "trend",      "panel": "main",
                   "params": {"period": 20},
                   "outputs": ["wma{period}"],              "fn": _calc_wma},
    "dema":       {"name": "DEMA",            "category": "trend",      "panel": "main",
                   "params": {"period": 20},
                   "outputs": ["dema{period}"],             "fn": _calc_dema},
    "supertrend": {"name": "SuperTrend",      "category": "trend",      "panel": "main",
                   "params": {"atr_period": 10, "multiplier": 3.0},
                   "outputs": ["supertrend", "supertrend_dir"], "fn": _calc_supertrend},
    "psar":       {"name": "Parabolic SAR",   "category": "trend",      "panel": "main",
                   "params": {"step": 0.02, "max": 0.2},
                   "outputs": ["psar"],                     "fn": _calc_psar},
    "ichimoku":   {"name": "Ichimoku Cloud",  "category": "trend",      "panel": "main",
                   "params": {"tenkan": 9, "kijun": 26, "senkou_b": 52},
                   "outputs": ["ichi_tenkan","ichi_kijun","ichi_senkou_a","ichi_senkou_b","ichi_chikou"],
                   "fn": _calc_ichimoku},
    "bb":         {"name": "Bollinger Bands", "category": "volatility", "panel": "main",
                   "params": {"period": 20, "std_dev": 2.0},
                   "outputs": ["bb_upper","bb_middle","bb_lower"],  "fn": _calc_bb},
    "keltner":    {"name": "Keltner Channel", "category": "volatility", "panel": "main",
                   "params": {"period": 20, "multiplier": 2.0},
                   "outputs": ["kc_upper","kc_middle","kc_lower"],  "fn": _calc_keltner},
    "donchian":   {"name": "Donchian Channel","category": "volatility", "panel": "main",
                   "params": {"period": 20},
                   "outputs": ["dc_upper","dc_middle","dc_lower"],  "fn": _calc_donchian},
    "vwap":       {"name": "VWAP",            "category": "volume",     "panel": "main",
                   "params": {},
                   "outputs": ["vwap"],                     "fn": _calc_vwap},
    "atr":        {"name": "ATR",             "category": "volatility", "panel": "sub",
                   "params": {"period": 14},
                   "outputs": ["atr{period}"],              "fn": _calc_atr},
    "rsi":        {"name": "RSI",             "category": "oscillator", "panel": "sub",
                   "params": {"period": 14},
                   "outputs": ["rsi{period}"],              "fn": _calc_rsi},
    "stoch":      {"name": "Stochastic",      "category": "oscillator", "panel": "sub",
                   "params": {"k": 14, "d": 3, "smooth": 3},
                   "outputs": ["stoch_k","stoch_d"],        "fn": _calc_stoch},
    "cci":        {"name": "CCI",             "category": "oscillator", "panel": "sub",
                   "params": {"period": 20},
                   "outputs": ["cci{period}"],              "fn": _calc_cci},
    "williams_r": {"name": "Williams %R",     "category": "oscillator", "panel": "sub",
                   "params": {"period": 14},
                   "outputs": ["willr{period}"],            "fn": _calc_williams_r},
    "macd":       {"name": "MACD",            "category": "momentum",   "panel": "sub",
                   "params": {"fast": 12, "slow": 26, "signal": 9},
                   "outputs": ["macd_line","macd_signal","macd_hist"], "fn": _calc_macd},
    "adx":        {"name": "ADX",             "category": "momentum",   "panel": "sub",
                   "params": {"period": 14},
                   "outputs": ["adx{period}","pdi","ndi"],  "fn": _calc_adx},
    "roc":        {"name": "ROC",             "category": "momentum",   "panel": "sub",
                   "params": {"period": 12},
                   "outputs": ["roc{period}"],              "fn": _calc_roc},
    "obv":        {"name": "OBV",             "category": "volume",     "panel": "sub",
                   "params": {},
                   "outputs": ["obv"],                      "fn": _calc_obv},
    "mfi":        {"name": "MFI",             "category": "volume",     "panel": "sub",
                   "params": {"period": 14},
                   "outputs": ["mfi{period}"],              "fn": _calc_mfi},
    "squeeze":    {"name": "Squeeze",         "category": "compound",   "panel": "sub",
                   "params": {"bb_period": 20, "kc_period": 20, "bb_std": 2.0, "kc_mult": 1.5},
                   "outputs": ["sqz_momentum","sqz_on"],    "fn": _calc_squeeze},
    "bb_pctb":    {"name": "BB %B",           "category": "compound",   "panel": "sub",
                   "params": {"period": 20, "std_dev": 2.0},
                   "outputs": ["bb_pctb"],                  "fn": _calc_bb_pctb},
}


def compute(df: pd.DataFrame, indicator_id: str, params: dict = None) -> dict:
    meta = REGISTRY.get(indicator_id)
    if not meta:
        raise ValueError(f"Unknown indicator: {indicator_id}")
    p = {**meta["params"], **(params or {})}
    return meta["fn"](df, p)


def compute_many(df: pd.DataFrame, requests: list) -> pd.DataFrame:
    """
    requests: [{"id": str, "params": dict}, ...]
    Returns df copy with indicator columns added (NaN left in place).
    """
    out = df.copy()
    for req in requests:
        cols = compute(df, req["id"], req.get("params", {}))
        for col, series in cols.items():
            out[col] = series
    return out
