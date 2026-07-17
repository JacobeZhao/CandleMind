"""
组合级回测：多品种共享资金、按时间对齐合并权益曲线 → 组合收益/回撤（捕捉同时回撤）。
固定权重分配（每品种 capital×weight 独立跑），权益曲线按时间合并求和。
使用 ML 趋势策略（backtest_ml_trend），每币阈值自动从 thresholds.json 加载。
"""
import numpy as np
import pandas as pd

from .ml_strategy import MLTrendParams, backtest_ml_trend
from .datafeed import load_klines

_INVOL_LOOKBACK_DAYS = 90


def _realized_vol(symbol, start, end) -> float | None:
    """日线收益率波动率（vol 比收益持久，可作前向配仓依据）。"""
    try:
        df = load_klines(symbol, "1d", start, end)
        v = float(df["close"].pct_change().dropna().std())
        return v if np.isfinite(v) and v > 0 else None
    except Exception:
        return None


def _equal_weights(symbols) -> dict:
    if not symbols:
        return {}
    weight = 1.0 / len(symbols)
    return {symbol: weight for symbol in symbols}


def _alloc_weights(symbols, start, end, mode) -> dict:
    if not symbols or mode != "invvol":
        return _equal_weights(symbols)

    backtest_start = pd.Timestamp(start).normalize()
    history_start = (
        backtest_start - pd.Timedelta(days=_INVOL_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    history_end = (backtest_start - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    vols = {symbol: _realized_vol(symbol, history_start, history_end) for symbol in symbols}
    if any(vol is None or not np.isfinite(vol) or vol <= 0 for vol in vols.values()):
        return _equal_weights(symbols)

    inverse = {symbol: 1.0 / vol for symbol, vol in vols.items()}
    total = sum(inverse.values())
    if not np.isfinite(total) or total <= 0:
        return _equal_weights(symbols)
    return {symbol: value / total for symbol, value in inverse.items()}


def run_portfolio_backtest(symbols, start, end, capital=10000,
                           risk_pct=0.01, alloc=None, alloc_mode="equal") -> dict:
    """
    组合回测：每币用 MLTrendParams.from_thresholds 自动加载阈值，无需手传 params。

    权益曲线计算：
      - 每币分配资金 cap_s = capital * weight
      - 每笔 pnl_dollars = pnl_r * cap_s * risk_pct
      - 各币权益曲线按 exit_time (ms) 索引，forward-fill 后求和成组合曲线

    Returns:
      equity_curve  : list of {time: ISO str, equity: float}
      per_symbol    : dict[symbol] -> {net_pct, max_drawdown, num_trades, total_r}
      metrics       : portfolio-level metrics dict
    """
    alloc = alloc or _alloc_weights(symbols, start, end, alloc_mode)
    curves: dict[str, dict] = {}
    per: dict[str, dict] = {}

    for s in symbols:
        weight = alloc.get(s, 1.0 / len(symbols))
        cap_s = capital * weight

        result = backtest_ml_trend(s, MLTrendParams.from_thresholds(s), start, end, False)

        if "error" in result:
            per[s] = {"net_pct": 0.0, "max_drawdown": 0.0, "num_trades": 0, "total_r": 0.0}
            curves[s] = {}
            continue

        trades = result["trades"]
        m = result["metrics"]

        # Build per-symbol equity curve keyed by exit_time int ms
        sym_equity: dict[int, float] = {}
        running = cap_s
        for t in trades:
            pnl_dollars = t.pnl_r * cap_s * risk_pct
            running += pnl_dollars
            sym_equity[int(t.exit_time)] = running

        # Per-symbol metrics (convert R-based metrics to dollar/pct context)
        final_s = running
        peak_s = cap_s
        mdd_s = 0.0
        eq_s = cap_s
        for t in trades:
            eq_s += t.pnl_r * cap_s * risk_pct
            peak_s = max(peak_s, eq_s)
            if peak_s > 0:
                mdd_s = max(mdd_s, (peak_s - eq_s) / peak_s * 100)

        per[s] = {
            "net_pct": round((final_s - cap_s) / cap_s * 100, 2) if cap_s > 0 else 0.0,
            "max_drawdown": round(mdd_s, 2),
            "num_trades": m.get("n_trades", 0),
            "total_r": m.get("total_r", 0.0),
        }
        curves[s] = sym_equity

    # Union of all timestamps, forward-fill per symbol, sum to portfolio equity
    all_times = sorted(set().union(*[set(c.keys()) for c in curves.values()])) if curves else []
    last = {s: capital * alloc.get(s, 1.0 / len(symbols)) for s in symbols}
    port = []
    for ts in all_times:
        for s in symbols:
            if ts in curves.get(s, {}):
                last[s] = curves[s][ts]
        iso_time = pd.Timestamp(ts, unit='ms').isoformat()
        port.append({"time": iso_time, "equity": round(sum(last.values()), 2)})

    # Portfolio-level metrics
    eqs = [p["equity"] for p in port if np.isfinite(p["equity"])]
    final = eqs[-1] if eqs else capital
    peak, max_dd = capital, 0.0
    for e in eqs:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100 if peak > 0 else 0)

    rets = np.diff(eqs) / np.array(eqs[:-1]) if len(eqs) > 1 else np.array([0.0])
    rets = rets[np.isfinite(rets)]
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if len(rets) and rets.std() > 0 else 0.0

    if len(port) > 1000:
        port = port[::len(port) // 1000]

    return {
        "equity_curve": port,
        "per_symbol": per,
        "metrics": {
            "total_return_pct": round((final - capital) / capital * 100, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
            "num_trades": sum(p["num_trades"] for p in per.values()),
            "final_equity": round(final, 2),
        },
    }
