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
                 tp_pct: float = 0.03,
                 fee_rate: float = 0.0004,
                 slippage_rate: float = 0.0005,
                 *,
                 taker_fee: float | None = None,
                 maker_fee: float = 0.0002,
                 fee_mult: float = 1.0,
                 use_funding: bool = False,
                 funding_rate: float = 0.0,
                 exec_mode: str = "market",
                 model_liq: bool = False) -> dict:
    """Run a bar backtest with signals executed at the next bar's open.

    Position quantity is risk-sized and capped by available notional
    (``capital * leverage``). Leverage never multiplies PnL or costs.

    ``fee_rate`` is the legacy taker-fee argument. ``taker_fee`` takes
    precedence when supplied. This OHLC engine cannot prove maker fills, so it
    applies taker fees for every execution and discloses that assumption.
    """

    effective_taker_fee = fee_rate if taker_fee is None else taker_fee
    non_negative_values = {
        "fee_rate": effective_taker_fee,
        "maker_fee": maker_fee,
        "slippage_rate": slippage_rate,
        "fee_mult": fee_mult,
    }
    if any(not np.isfinite(value) or value < 0
           for value in non_negative_values.values()):
        raise ValueError("fees, slippage_rate, and fee_mult must be non-negative")
    if not np.isfinite(funding_rate):
        raise ValueError("funding_rate must be finite")
    if not np.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not np.isfinite(leverage) or leverage <= 0:
        raise ValueError("leverage must be positive")

    applied_fee_rate = effective_taker_fee * fee_mult
    requested_exec_mode = str(exec_mode).lower()
    execution = {
        "signal_timing": "next_open",
        "requested_mode": requested_exec_mode,
        "fee_liquidity": "taker",
        "taker_fee_rate": effective_taker_fee,
        "maker_fee_rate": maker_fee,
        "applied_fee_rate": applied_fee_rate,
        "fee_mult": fee_mult,
        "maker_fee_applied": False,
        "maker_fallback_reason": (
            "OHLC bars do not identify maker fills; taker fees are applied."
        ),
        "leverage_role": "margin_and_max_quantity_only",
        "model_liquidation_requested": bool(model_liq),
        "model_liquidation_applied": False,
    }

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
    signals = pd.Series(sig_fn(df, strategy_params), index=df.index).fillna(0)

    if df.empty:
        return {
            "trades": [],
            "equity_curve": [],
            "metrics": {
                "total_return": 0,
                "total_return_pct": 0,
                "num_trades": 0,
                "win_rate": 0,
                "max_drawdown": 0,
                "sharpe": 0,
                "avg_pnl": 0,
                "gross_return": 0,
                "gross_return_pct": 0,
                "total_fees": 0,
                "slippage_cost": 0,
                "total_funding": 0,
                "total_cost": 0,
            },
            "execution": execution,
        }

    # 行情自适应止损止盈（ATR + 结构位）预计算
    atr_n   = int(strategy_params.get("atr_period_sl", 14))
    sl_mult = float(strategy_params.get("atr_sl_mult", 2.0))
    swing   = int(strategy_params.get("swing_lookback", 10))
    default_rr = tp_pct / sl_pct if sl_pct > 0 else 2.0
    rr      = float(strategy_params.get("rr_ratio", default_rr))
    atr_arr   = _atr(df, atr_n).to_numpy()
    slow_arr  = df["low"].rolling(swing).min().to_numpy()
    shigh_arr = df["high"].rolling(swing).max().to_numpy()

    capital  = initial_capital
    trades   = []
    equity   = []
    equity_values = []
    position = None
    total_fees = 0.0
    total_slippage = 0.0
    total_funding = 0.0
    gross_return = 0.0

    def _time_at(index):
        return pd.Timestamp(df.iloc[index]["open_time"]).isoformat()

    def _fill_price(reference_price, action):
        direction = 1.0 if action == "BUY" else -1.0
        return reference_price * (1.0 + direction * slippage_rate)

    def _funding_cost(current, time_value):
        if not use_funding or funding_rate == 0:
            return 0.0
        elapsed = pd.Timestamp(time_value) - current["entry_timestamp"]
        holding_hours = max(elapsed.total_seconds() / 3600.0, 0.0)
        direction = 1.0 if current["side"] == "LONG" else -1.0
        entry_notional = current["entry_reference_price"] * current["qty"]
        return direction * entry_notional * funding_rate * holding_hours / 8.0

    def _open_position(side, execution_index, signal_index):
        nonlocal capital, position, total_fees, total_slippage

        reference_price = float(df.iloc[execution_index]["open"])
        action = "BUY" if side == "LONG" else "SELL"
        fill_price = _fill_price(reference_price, action)

        atr_value = float(atr_arr[signal_index])
        if np.isfinite(atr_value) and atr_value > 0:
            stop_offset = sl_mult * atr_value
        else:
            stop_offset = reference_price * sl_pct

        if side == "LONG":
            structural_stop = float(slow_arr[signal_index])
            atr_stop = reference_price - stop_offset
            stop_price = (min(structural_stop, atr_stop)
                          if np.isfinite(structural_stop) else atr_stop)
            take_profit = reference_price + rr * (reference_price - stop_price)
        else:
            structural_stop = float(shigh_arr[signal_index])
            atr_stop = reference_price + stop_offset
            stop_price = (max(structural_stop, atr_stop)
                          if np.isfinite(structural_stop) else atr_stop)
            take_profit = reference_price - rr * (stop_price - reference_price)

        stop_distance = abs(reference_price - stop_price)
        risk_qty = ((capital * risk_pct) / stop_distance
                    if stop_distance > 0 else 1.0)
        max_qty = (capital * leverage) / reference_price
        qty = min(risk_qty, max_qty)
        entry_fee = fill_price * qty * applied_fee_rate
        entry_slippage = abs(fill_price - reference_price) * qty
        capital -= entry_fee
        total_fees += entry_fee
        total_slippage += entry_slippage
        position = {
            "side": side,
            "entry_time": _time_at(execution_index),
            "entry_timestamp": pd.Timestamp(df.iloc[execution_index]["open_time"]),
            "entry_reference_price": reference_price,
            "entry_price": fill_price,
            "entry_fee": entry_fee,
            "entry_slippage": entry_slippage,
            "qty": qty,
            "sl": stop_price,
            "tp": take_profit,
        }

    def _close_position(reference_price, reason, time_value):
        nonlocal capital, position, total_fees, total_slippage
        nonlocal total_funding, gross_return

        current = position
        action = "SELL" if current["side"] == "LONG" else "BUY"
        fill_price = _fill_price(reference_price, action)
        qty = current["qty"]
        direction = 1.0 if current["side"] == "LONG" else -1.0
        gross_pnl = (
            direction
            * (reference_price - current["entry_reference_price"])
            * qty
        )
        price_pnl = (
            direction
            * (fill_price - current["entry_price"])
            * qty
        )
        exit_fee = fill_price * qty * applied_fee_rate
        exit_slippage = abs(fill_price - reference_price) * qty
        funding_cost = _funding_cost(current, time_value)
        fees = current["entry_fee"] + exit_fee
        slippage_cost = current["entry_slippage"] + exit_slippage
        pnl = price_pnl - fees - funding_cost

        # Entry fee was deducted when the position opened.
        capital += price_pnl - exit_fee - funding_cost
        total_fees += exit_fee
        total_slippage += exit_slippage
        total_funding += funding_cost
        gross_return += gross_pnl
        holding_hours = max(
            (pd.Timestamp(time_value) - current["entry_timestamp"])
            .total_seconds() / 3600.0,
            0.0,
        )
        trades.append({
            "entry_time": current["entry_time"],
            "exit_time": time_value,
            "side": current["side"],
            "entry_price": round(current["entry_price"], 4),
            "exit_price": round(fill_price, 4),
            "qty": round(qty, 8),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / initial_capital * 100, 4),
            "exit_reason": reason,
            "gross_pnl": round(gross_pnl, 4),
            "entry_fee": round(current["entry_fee"], 4),
            "exit_fee": round(exit_fee, 4),
            "fees": round(fees, 4),
            "slippage_cost": round(slippage_cost, 4),
            "funding_cost": round(funding_cost, 4),
            "holding_hours": round(holding_hours, 4),
            "total_cost": round(fees + slippage_cost + funding_cost, 4),
        })
        position = None

    for i in range(len(df)):
        row = df.iloc[i]
        time_value = _time_at(i)
        open_price = float(row["open"])
        close_price = float(row["close"])
        high_price = float(row["high"])
        low_price = float(row["low"])

        if i > 0:
            raw_signal = float(signals.iloc[i - 1])
            pending_signal = 1 if raw_signal > 0 else -1 if raw_signal < 0 else 0

            # A standing stop triggered by a gap fills at the open, never at the
            # more favorable stop level.
            if position:
                gap_stop = (
                    position["side"] == "LONG" and open_price <= position["sl"]
                ) or (
                    position["side"] == "SHORT" and open_price >= position["sl"]
                )
                if gap_stop:
                    _close_position(open_price, "止损", time_value)

            if position and (
                (position["side"] == "LONG" and pending_signal == -1)
                or (position["side"] == "SHORT" and pending_signal == 1)
            ):
                _close_position(open_price, "反转", time_value)

            if position is None and pending_signal != 0:
                side = "LONG" if pending_signal == 1 else "SHORT"
                _open_position(side, i, i - 1)

            # Stop-first ordering is conservative when both levels trade in one bar.
            if position:
                if position["side"] == "LONG":
                    if low_price <= position["sl"]:
                        _close_position(position["sl"], "止损", time_value)
                    elif high_price >= position["tp"]:
                        _close_position(position["tp"], "止盈", time_value)
                else:
                    if high_price >= position["sl"]:
                        _close_position(position["sl"], "止损", time_value)
                    elif low_price <= position["tp"]:
                        _close_position(position["tp"], "止盈", time_value)

        marked_equity = capital
        if position:
            direction = 1.0 if position["side"] == "LONG" else -1.0
            marked_equity += (
                direction
                * (close_price - position["entry_price"])
                * position["qty"]
            )
            marked_equity -= _funding_cost(position, time_value)
        equity_values.append(marked_equity)
        equity.append({"time": time_value, "equity": round(marked_equity, 2)})

    if position:
        last_index = len(df) - 1
        _close_position(
            float(df.iloc[last_index]["close"]),
            "期末强平",
            _time_at(last_index),
        )
        equity_values[-1] = capital
        equity[-1]["equity"] = round(capital, 2)

    peak = initial_capital
    max_dd = 0.0
    for marked_equity in equity_values:
        peak = max(peak, marked_equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - marked_equity) / peak * 100)

    if len(equity) > 1000:
        sample_indices = np.linspace(0, len(equity) - 1, num=1000, dtype=int)
        equity = [equity[index] for index in sample_indices]

    n = len(trades)
    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    total_return = capital - initial_capital
    pnl_pcts = [trade["pnl_pct"] for trade in trades]
    pnl_std = float(np.std(pnl_pcts)) if pnl_pcts else 0.0
    sharpe = (
        float(np.mean(pnl_pcts) / pnl_std) * np.sqrt(252)
        if pnl_std > 0 else 0.0
    )
    metrics = {
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return / initial_capital * 100, 2),
        "num_trades": n,
        "win_rate": round(wins / n * 100, 2) if n else 0,
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "avg_pnl": round(sum(trade["pnl"] for trade in trades) / n, 4) if n else 0,
        "gross_return": round(gross_return, 2),
        "gross_return_pct": round(gross_return / initial_capital * 100, 2),
        "total_fees": round(total_fees, 4),
        "slippage_cost": round(total_slippage, 4),
        "total_funding": round(total_funding, 4),
        "total_cost": round(total_fees + total_slippage + total_funding, 4),
    }

    return {
        "trades": trades,
        "equity_curve": equity,
        "metrics": metrics,
        "execution": execution,
    }
