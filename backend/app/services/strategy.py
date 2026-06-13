"""
Strategy engine: fetch klines + compute indicators + generate signal.
Supports all built-in strategy types and AI-parsed condition strategies.
"""
import json
import numpy as np
import pandas as pd
from binance.client import Client

from .indicators import REGISTRY, compute_many, _atr


# ── 行情自适应止损止盈（ATR + 结构位结合）─────────────────────────────────────

def adaptive_sl_tp(df: pd.DataFrame, side: str, entry: float, params: dict) -> tuple:
    """
    止损 = 结构位(近 swing_lookback 根摆动低/高) 与 ATR 止损 中"更远"的一个（防被扫）。
    止盈 = 入场 ± rr_ratio × 止损距离（盈亏比，随自适应止损伸缩）。
    返回 (sl_price, tp_price)。
    """
    atr_n = int(params.get("atr_period_sl", 14))
    mult  = float(params.get("atr_sl_mult", 2.0))
    swing = int(params.get("swing_lookback", 10))
    rr    = float(params.get("rr_ratio", 2.0))

    atr = float(_atr(df, atr_n).iloc[-1])
    if not (atr > 0):
        atr = entry * 0.005

    if side == "LONG":
        struct   = float(df["low"].iloc[-swing:].min())
        atr_stop = entry - mult * atr
        sl = min(struct, atr_stop)              # 更远（更低）
        tp = entry + rr * (entry - sl)
    else:
        struct   = float(df["high"].iloc[-swing:].max())
        atr_stop = entry + mult * atr
        sl = max(struct, atr_stop)              # 更远（更高）
        tp = entry - rr * (sl - entry)
    return sl, tp

# ── Indicator requirements per strategy type ──────────────────────────────────
# Maps strategy_type → list of indicator requests needed for signal generation

_STRATEGY_INDICATORS = {
    "supertrend":  [{"id": "supertrend", "params": {}}],
    "ema_cross":   [],   # computed inline from close price
    "rsi":         [{"id": "rsi",  "params": {}}],
    "macd":        [{"id": "macd", "params": {}}],
    "bb_breakout": [{"id": "bb",   "params": {}}],
    "adx_trend":   [{"id": "adx",  "params": {}}, {"id": "supertrend", "params": {}}],
    "stoch_cross": [{"id": "stoch","params": {}}],
    "cci_cross":   [{"id": "cci",  "params": {}}],
    "roc":         [{"id": "roc",  "params": {}}],
    "conditions":  [],   # AI strategy — indicators specified in params
}


def _base_df(client: Client, symbol: str, interval: str, limit: int) -> pd.DataFrame:
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df[["open", "high", "low", "close", "volume"]] = \
        df[["open", "high", "low", "close", "volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def fetch_klines(client: Client, symbol: str, interval: str, limit: int = 200,
                 indicator_requests: list = None) -> pd.DataFrame:
    df = _base_df(client, symbol, interval, limit)
    reqs = indicator_requests or [{"id": "supertrend", "params": {}}]
    df = compute_many(df, reqs)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def fetch_klines_for_strategy(client: Client, symbol: str, interval: str,
                               strategy_type: str, strategy_params: dict,
                               ai_strategy_json: str = None,
                               limit: int = 200) -> pd.DataFrame:
    """Fetch klines with the correct indicators for the given strategy type."""
    df = _base_df(client, symbol, interval, limit)

    # Determine which indicators to compute
    reqs = []
    if ai_strategy_json:
        # AI strategy: use indicators listed in the parsed JSON
        try:
            parsed = json.loads(ai_strategy_json)
            reqs = parsed.get("indicators_needed", [])
        except Exception:
            reqs = [{"id": "supertrend", "params": {}}]
    else:
        base_reqs = _STRATEGY_INDICATORS.get(strategy_type, [{"id": "supertrend", "params": {}}])
        # Merge strategy_params into indicator params
        for req in base_reqs:
            merged_params = {**req.get("params", {})}
            # Apply strategy params where they overlap
            if req["id"] == strategy_type or req["id"] in strategy_type:
                merged_params.update(strategy_params)
            reqs.append({"id": req["id"], "params": merged_params})

    if not reqs:
        reqs = [{"id": "supertrend", "params": {}}]

    df = compute_many(df, reqs)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ── Condition evaluator ───────────────────────────────────────────────────────

def _eval_condition(row: pd.Series, prev: pd.Series, cond: dict) -> bool:
    col   = cond.get("column", "")
    c     = cond.get("condition", "")
    val   = cond.get("value")
    vs    = cond.get("vs_column", "")

    cur_v  = row.get(col, np.nan)
    prev_v = prev.get(col, np.nan)
    vs_cur = row.get(vs,  np.nan) if vs else np.nan
    vs_prv = prev.get(vs, np.nan) if vs else np.nan
    tgt    = vs_cur if vs and not np.isnan(float(vs_cur)) else (val if val is not None else np.nan)
    tgt_p  = vs_prv if vs and not np.isnan(float(vs_prv)) else (val if val is not None else np.nan)

    try:
        if c == "cross_above": return float(prev_v) <= float(tgt_p) and float(cur_v) > float(tgt)
        if c == "cross_below": return float(prev_v) >= float(tgt_p) and float(cur_v) < float(tgt)
        if c == "above":       return float(cur_v) > float(tgt)
        if c == "below":       return float(cur_v) < float(tgt)
        if c == "rising":      return float(cur_v) > float(prev_v)
        if c == "falling":     return float(cur_v) < float(prev_v)
        if c == "equals":      return int(float(cur_v)) == int(val) if val is not None else False
    except (TypeError, ValueError):
        return False
    return False


def _eval_conditions(row, prev, conditions: list) -> bool:
    return all(_eval_condition(row, prev, c) for c in conditions)


# ── Signal generators per strategy type ──────────────────────────────────────

def _sig_supertrend(df: pd.DataFrame, params: dict) -> str:
    prev_dir = int(df.iloc[-2].get("supertrend_dir", 0))
    curr_dir = int(df.iloc[-1].get("supertrend_dir", 0))
    if prev_dir == -1 and curr_dir == 1:  return "LONG"
    if prev_dir ==  1 and curr_dir == -1: return "SHORT"
    return "NONE"

def _sig_ema_cross(df: pd.DataFrame, params: dict) -> str:
    from .indicators import _ema
    fast_n = params.get("fast", 12)
    slow_n = params.get("slow", 26)
    fast = _ema(df["close"], fast_n)
    slow = _ema(df["close"], slow_n)
    if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]: return "LONG"
    if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]: return "SHORT"
    return "NONE"

def _sig_rsi(df: pd.DataFrame, params: dict) -> str:
    n   = params.get("period", 14)
    col = f"rsi{n}"
    os_ = params.get("oversold", 30)
    ob_ = params.get("overbought", 70)
    if col not in df.columns: return "NONE"
    prev, curr = df[col].iloc[-2], df[col].iloc[-1]
    if prev < os_ and curr >= os_: return "LONG"
    if prev > ob_ and curr <= ob_: return "SHORT"
    return "NONE"

def _sig_macd(df: pd.DataFrame, params: dict) -> str:
    if "macd_line" not in df.columns: return "NONE"
    ml_p, ms_p = df["macd_line"].iloc[-2], df["macd_signal"].iloc[-2]
    ml_c, ms_c = df["macd_line"].iloc[-1], df["macd_signal"].iloc[-1]
    if ml_p <= ms_p and ml_c > ms_c: return "LONG"
    if ml_p >= ms_p and ml_c < ms_c: return "SHORT"
    return "NONE"

def _sig_bb_breakout(df: pd.DataFrame, params: dict) -> str:
    if "bb_upper" not in df.columns: return "NONE"
    close = df["close"].iloc[-1]
    if close > df["bb_upper"].iloc[-1]: return "LONG"
    if close < df["bb_lower"].iloc[-1]: return "SHORT"
    return "NONE"

def _sig_adx_trend(df: pd.DataFrame, params: dict) -> str:
    n   = params.get("period", 14)
    thr = params.get("adx_threshold", 25)
    col = f"adx{n}"
    if col not in df.columns or "supertrend_dir" not in df.columns: return "NONE"
    adx = df[col].iloc[-1]
    if adx < thr: return "NONE"
    d = int(df["supertrend_dir"].iloc[-1])
    return "LONG" if d == 1 else "SHORT"

def _sig_stoch_cross(df: pd.DataFrame, params: dict) -> str:
    if "stoch_k" not in df.columns: return "NONE"
    kp, dp = df["stoch_k"].iloc[-2], df["stoch_d"].iloc[-2]
    kc, dc = df["stoch_k"].iloc[-1], df["stoch_d"].iloc[-1]
    ob_ = params.get("overbought", 80)
    if kp <= dp and kc > dc and kc < ob_: return "LONG"
    if kp >= dp and kc < dc and kc > params.get("oversold", 20): return "SHORT"
    return "NONE"

def _sig_cci(df: pd.DataFrame, params: dict) -> str:
    n   = params.get("period", 20)
    lev = params.get("level", 100)
    col = f"cci{n}"
    if col not in df.columns: return "NONE"
    prev, curr = df[col].iloc[-2], df[col].iloc[-1]
    if prev < -lev and curr >= -lev: return "LONG"
    if prev >  lev and curr <=  lev: return "SHORT"
    return "NONE"

def _sig_roc(df: pd.DataFrame, params: dict) -> str:
    n   = params.get("period", 12)
    col = f"roc{n}"
    if col not in df.columns: return "NONE"
    prev, curr = df[col].iloc[-2], df[col].iloc[-1]
    if prev < 0 and curr >= 0: return "LONG"
    if prev > 0 and curr <= 0: return "SHORT"
    return "NONE"

def _sig_conditions(df: pd.DataFrame, params: dict) -> str:
    """AI-parsed condition strategy."""
    row, prev = df.iloc[-1], df.iloc[-2]
    if _eval_conditions(row, prev, params.get("entry_long",  [])): return "LONG"
    if _eval_conditions(row, prev, params.get("entry_short", [])): return "SHORT"
    return "NONE"


_SIGNAL_FNS = {
    "supertrend":  _sig_supertrend,
    "ema_cross":   _sig_ema_cross,
    "rsi":         _sig_rsi,
    "macd":        _sig_macd,
    "bb_breakout": _sig_bb_breakout,
    "adx_trend":   _sig_adx_trend,
    "stoch_cross": _sig_stoch_cross,
    "cci_cross":   _sig_cci,
    "roc":         _sig_roc,
    "conditions":  _sig_conditions,
}


def get_signal(df: pd.DataFrame) -> str:
    """Legacy helper — SuperTrend only."""
    return _sig_supertrend(df, {})


def get_signal_for_strategy(df: pd.DataFrame, strategy_type: str,
                             strategy_params: dict,
                             ai_strategy_json: str = None) -> str:
    """Unified signal entry point for all strategy types."""
    if len(df) < 2:
        return "NONE"

    if ai_strategy_json:
        try:
            parsed = json.loads(ai_strategy_json)
            strat  = parsed.get("strategy", {})
            params = {
                "entry_long":  strat.get("entry_long",  []),
                "entry_short": strat.get("entry_short", []),
            }
            return _sig_conditions(df, params)
        except Exception:
            pass

    fn = _SIGNAL_FNS.get(strategy_type, _sig_supertrend)
    return fn(df, strategy_params)
