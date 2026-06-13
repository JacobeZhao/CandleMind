"""
Backtest engine.
Supports: supertrend, ema_cross, rsi, macd, bb_breakout, adx_trend,
          stoch_cross, cci_cross, roc_momentum, and AI conditions format.
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path
import os

from .indicators import REGISTRY, compute_many, _atr
from ..datastore import KLINES_DIR as DATA_DIR


# ── Historical data (cached) ──────────────────────────────────────────────────

def cache_path(symbol, interval, start_date, end_date) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{symbol}_{interval}_{start_date}_{end_date}.json"


def fetch_historical(client, symbol: str, interval: str,
                     start_date: str, end_date: str,
                     batch_delay: float = 0.25) -> pd.DataFrame:
    """
    分批拉取历史 K 线并缓存到本地。
    batch_delay: 每批次之间的等待秒数，避免触发限流（默认 250ms）。
    """
    import time
    from datetime import datetime
    cp = cache_path(symbol, interval, start_date, end_date)
    if cp.exists():
        import json as _json
        raw = _json.loads(cp.read_text())
    else:
        start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        end_ts   = int(datetime.strptime(end_date,   "%Y-%m-%d").timestamp() * 1000)
        raw, cur, batch_num = [], start_ts, 0
        while cur < end_ts:
            # 每批次之间休眠，避免触发 IP 限流
            if batch_num > 0:
                time.sleep(batch_delay)
            batch = client.futures_klines(
                symbol=symbol, interval=interval,
                startTime=cur, endTime=end_ts, limit=1000,
            )
            if not batch:
                break
            raw.extend(batch)
            cur = batch[-1][6] + 1
            batch_num += 1
            if len(batch) < 1000:
                break
        cp.write_text(json.dumps(raw))

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df[["open", "high", "low", "close", "volume"]] = \
        df[["open", "high", "low", "close", "volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.reset_index(drop=True, inplace=True)
    return df


# ── Signal generators ─────────────────────────────────────────────────────────

def _ema(s, n): return s.ewm(span=n, adjust=False).mean()

def _signals_supertrend(df, p):
    from .indicators import _calc_supertrend
    cols = _calc_supertrend(df, p)
    d    = cols["supertrend_dir"]
    sig  = pd.Series(0, index=df.index)
    sig[(d.shift(1) == -1) & (d == 1)]  =  1
    sig[(d.shift(1) ==  1) & (d == -1)] = -1
    return sig

def _signals_ema_cross(df, p):
    fast = _ema(df["close"], p.get("fast", 12))
    slow = _ema(df["close"], p.get("slow", 26))
    sig  = pd.Series(0, index=df.index)
    sig[(fast.shift(1) <= slow.shift(1)) & (fast > slow)] =  1
    sig[(fast.shift(1) >= slow.shift(1)) & (fast < slow)] = -1
    return sig

def _signals_rsi(df, p):
    from .indicators import _calc_rsi
    n    = p.get("period", 14)
    rsi  = _calc_rsi(df, p)[f"rsi{n}"]
    os_, ob_ = p.get("oversold", 30), p.get("overbought", 70)
    sig  = pd.Series(0, index=df.index)
    sig[(rsi.shift(1) < os_) & (rsi >= os_)] =  1
    sig[(rsi.shift(1) > ob_) & (rsi <= ob_)] = -1
    return sig

def _signals_macd(df, p):
    from .indicators import _calc_macd
    cols = _calc_macd(df, p)
    line, sig_line = cols["macd_line"], cols["macd_signal"]
    sig  = pd.Series(0, index=df.index)
    sig[(line.shift(1) <= sig_line.shift(1)) & (line > sig_line)] =  1
    sig[(line.shift(1) >= sig_line.shift(1)) & (line < sig_line)] = -1
    return sig

def _signals_bb_breakout(df, p):
    from .indicators import _calc_bb
    bb   = _calc_bb(df, p)
    sig  = pd.Series(0, index=df.index)
    sig[df["close"] > bb["bb_upper"]] =  1
    sig[df["close"] < bb["bb_lower"]] = -1
    return sig

def _signals_adx_trend(df, p):
    from .indicators import _calc_adx, _calc_supertrend
    n    = p.get("period", 14)
    adx  = _calc_adx(df, p)[f"adx{n}"]
    st   = _calc_supertrend(df, {"atr_period": 10, "multiplier": 3.0})
    d    = st["supertrend_dir"]
    sig  = pd.Series(0, index=df.index)
    trend_strong = adx > p.get("adx_threshold", 25)
    sig[(d == 1)  & trend_strong] =  1
    sig[(d == -1) & trend_strong] = -1
    return sig

def _signals_stoch_cross(df, p):
    from .indicators import _calc_stoch
    cols = _calc_stoch(df, p)
    k, d = cols["stoch_k"], cols["stoch_d"]
    os_, ob_ = p.get("oversold", 20), p.get("overbought", 80)
    sig  = pd.Series(0, index=df.index)
    sig[(k.shift(1) <= d.shift(1)) & (k > d) & (k < ob_)] =  1
    sig[(k.shift(1) >= d.shift(1)) & (k < d) & (k > os_)] = -1
    return sig

def _signals_cci_cross(df, p):
    from .indicators import _calc_cci
    n    = p.get("period", 20)
    cci  = _calc_cci(df, p)[f"cci{n}"]
    lev  = p.get("level", 100)
    sig  = pd.Series(0, index=df.index)
    sig[(cci.shift(1) < -lev) & (cci >= -lev)] =  1
    sig[(cci.shift(1) >  lev) & (cci <=  lev)] = -1
    return sig

def _signals_roc(df, p):
    from .indicators import _calc_roc
    n    = p.get("period", 12)
    roc  = _calc_roc(df, p)[f"roc{n}"]
    sig  = pd.Series(0, index=df.index)
    sig[(roc.shift(1) < 0) & (roc >= 0)] =  1
    sig[(roc.shift(1) > 0) & (roc <= 0)] = -1
    return sig


# Conditions-based signal (for AI-parsed strategies)
def _signals_conditions(df, p):
    """
    p must contain {"entry_long": [...], "entry_short": [...]}
    Each condition: {"column": str, "condition": str, "value": optional, "vs_column": optional}
    """
    from .strategy import _eval_conditions
    entry_long  = p.get("entry_long",  [])
    entry_short = p.get("entry_short", [])
    sig = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        row, prev = df.iloc[i], df.iloc[i - 1]
        if _eval_conditions(row, prev, entry_long):  sig.iloc[i] =  1
        if _eval_conditions(row, prev, entry_short): sig.iloc[i] = -1
    return sig


_SIGNAL_FNS = {
    "supertrend":   (_signals_supertrend,   [{"id": "supertrend",  "params": {}}]),
    "ema_cross":    (_signals_ema_cross,     []),
    "rsi":          (_signals_rsi,           []),
    "macd":         (_signals_macd,          [{"id": "macd",       "params": {}}]),
    "bb_breakout":  (_signals_bb_breakout,   [{"id": "bb",         "params": {}}]),
    "adx_trend":    (_signals_adx_trend,     [{"id": "adx",        "params": {}},
                                              {"id": "supertrend", "params": {}}]),
    "stoch_cross":  (_signals_stoch_cross,   [{"id": "stoch",      "params": {}}]),
    "cci_cross":    (_signals_cci_cross,     [{"id": "cci",        "params": {}}]),
    "roc":          (_signals_roc,           [{"id": "roc",        "params": {}}]),
    "conditions":   (_signals_conditions,    []),  # AI-parsed
}


# ── Backtest engine ───────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame,
                 strategy_type: str,
                 strategy_params: dict,
                 initial_capital: float = 10000,
                 leverage: int = 1,
                 risk_pct: float = 0.01,
                 sl_pct: float = 0.015,
                 tp_pct: float = 0.03) -> dict:

    entry = _SIGNAL_FNS.get(strategy_type)
    if not entry:
        raise ValueError(f"不支持的策略: {strategy_type}")

    sig_fn, precompute = entry

    # Pre-compute required indicators
    if precompute:
        df = compute_many(df, precompute)
    # For AI conditions, pre-compute indicators listed in params
    if strategy_type == "conditions" and "indicators_needed" in strategy_params:
        df = compute_many(df, strategy_params["indicators_needed"])

    df = df.dropna().reset_index(drop=True)
    signals  = sig_fn(df, strategy_params)

    # 行情自适应止损止盈（ATR + 结构位）预计算
    atr_n   = int(strategy_params.get("atr_period_sl", 14))
    sl_mult = float(strategy_params.get("atr_sl_mult", 2.0))
    swing   = int(strategy_params.get("swing_lookback", 10))
    rr      = float(strategy_params.get("rr_ratio", 2.0))
    atr_arr   = _atr(df, atr_n).to_numpy()
    slow_arr  = df["low"].rolling(swing).min().to_numpy()
    shigh_arr = df["high"].rolling(swing).max().to_numpy()

    capital  = initial_capital
    trades   = []
    equity   = []
    position = None

    for i in range(1, len(df)):
        row   = df.iloc[i]
        t     = row["open_time"].isoformat()
        close = float(row["close"])
        high  = float(row["high"])
        low   = float(row["low"])

        if position:
            ep, er = None, None
            if position["side"] == "LONG":
                if low  <= position["sl"]: ep, er = position["sl"], "止损"
                elif high >= position["tp"]: ep, er = position["tp"], "止盈"
            else:
                if high >= position["sl"]: ep, er = position["sl"], "止损"
                elif low  <= position["tp"]: ep, er = position["tp"], "止盈"

            new_sig = int(signals.iloc[i])
            if ep is None and (
                (position["side"] == "LONG"  and new_sig == -1) or
                (position["side"] == "SHORT" and new_sig ==  1)
            ):
                ep, er = close, "反转"

            if ep:
                qty = position["qty"]
                if position["side"] == "LONG":
                    pnl = (ep - position["entry_price"]) * qty * leverage
                else:
                    pnl = (position["entry_price"] - ep) * qty * leverage
                capital += pnl
                trades.append({
                    "entry_time":  position["entry_time"],
                    "exit_time":   t,
                    "side":        position["side"],
                    "entry_price": round(position["entry_price"], 4),
                    "exit_price":  round(ep, 4),
                    "pnl":         round(pnl, 4),
                    "pnl_pct":     round(pnl / initial_capital * 100, 4),
                    "exit_reason": er,
                })
                position = None

        sig = int(signals.iloc[i])
        if not position and sig != 0:
            side  = "LONG" if sig == 1 else "SHORT"
            atr_i = atr_arr[i] if atr_arr[i] > 0 else close * 0.005
            if side == "LONG":
                struct = slow_arr[i]
                sl = min(struct, close - sl_mult * atr_i) if struct == struct else close - sl_mult * atr_i
                tp = close + rr * (close - sl)
            else:
                struct = shigh_arr[i]
                sl = max(struct, close + sl_mult * atr_i) if struct == struct else close + sl_mult * atr_i
                tp = close - rr * (sl - close)
            stop_dist = abs(close - sl)
            qty = (capital * risk_pct) / stop_dist if stop_dist > 0 else 1
            position = {"side": side, "entry_time": t, "entry_price": close,
                        "qty": qty, "sl": sl, "tp": tp}

        equity.append({"time": t, "equity": round(capital, 2)})

    if len(equity) > 1000:
        step   = len(equity) // 1000
        equity = equity[::step]

    n = len(trades)
    if n == 0:
        metrics = dict(total_return=0, total_return_pct=0, num_trades=0,
                       win_rate=0, max_drawdown=0, sharpe=0, avg_pnl=0)
    else:
        wins    = sum(1 for t in trades if t["pnl"] > 0)
        total_r = capital - initial_capital
        eqs     = [e["equity"] for e in equity]
        peak, max_dd = initial_capital, 0.0
        for eq in eqs:
            peak   = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak * 100)
        pcts   = [t["pnl_pct"] for t in trades]
        sharpe = (float(np.mean(pcts) / np.std(pcts)) * np.sqrt(252)
                  if np.std(pcts) > 0 else 0)
        metrics = {
            "total_return":     round(total_r, 2),
            "total_return_pct": round(total_r / initial_capital * 100, 2),
            "num_trades":       n,
            "win_rate":         round(wins / n * 100, 2),
            "max_drawdown":     round(max_dd, 2),
            "sharpe":           round(sharpe, 2),
            "avg_pnl":          round(sum(t["pnl"] for t in trades) / n, 4),
        }

    return {"trades": trades, "equity_curve": equity, "metrics": metrics}
