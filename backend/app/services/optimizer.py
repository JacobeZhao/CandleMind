"""
参数优化器：网格搜索，以样本外(OOS)评判 + 过拟合惩罚（IS/OOS 差距），多线程加速。
基于 ML 趋势策略（backtest_ml_trend），阈值自动从 thresholds.json 按币加载。
得分 = OOS均R − overfit_penalty × |IS均R − OOS均R|，按得分降序。
"""
import itertools
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

from .ml_strategy import MLTrendParams, backtest_ml_trend
from . import experiments


def _avg_r(trades, pred):
    """从 trade list 中筛选满足 pred 的交易，返回平均 pnl_r（无交易返回 0.0）。"""
    subset = [t for t in trades if pred(t)]
    if not subset:
        return 0.0
    return round(sum(t.pnl_r for t in subset) / len(subset), 4)


def grid_search(symbols, base_params, grid: dict, start, end, split_date,
                risk_pct=0.01, overfit_penalty=0.5, max_workers=4, log=True) -> dict:
    """
    网格搜索 MLTrendParams 字段组合，以 OOS 表现选优，防过拟合。

    Parameters
    ----------
    symbols       : list[str]  — 币种列表
    base_params   : ignored    — 入场阈值自动按币从 thresholds.json 加载
    grid          : dict       — 待搜索的 MLTrendParams 字段及候选值列表
                                 可用 key: exit_threshold, reversal_threshold,
                                           atr_trail, max_adds, add_atr_dist, kelly_frac
    start, end    : str        — 回测时间范围（"YYYY-MM-DD"）
    split_date    : str        — IS/OOS 分割日期
    risk_pct      : float      — 每笔风险占账户比例
    overfit_penalty: float     — 过拟合惩罚系数
    max_workers   : int        — 并行线程数
    log           : bool       — 是否记录到 experiments

    Returns
    -------
    dict with keys: keys, tested, results (top 50), best
    """
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))

    split_ts_ms = int(pd.Timestamp(split_date).timestamp() * 1000)

    def _eval(combo):
        kw = dict(zip(keys, combo))

        is_rs, oos_rs = [], []
        for sym in symbols:
            try:
                params = MLTrendParams.from_thresholds(sym, risk_pct=risk_pct, **kw)
                result = backtest_ml_trend(sym, params, start, end, verbose=False)
                if "error" in result:
                    raise ValueError(result["error"])
                trades = result.get("trades", [])
                is_avg  = _avg_r(trades, lambda t: t.exit_time < split_ts_ms)
                oos_avg = _avg_r(trades, lambda t: t.exit_time >= split_ts_ms)
                is_rs.append(is_avg)
                oos_rs.append(oos_avg)
            except Exception:
                is_rs.append(0.0)
                oos_rs.append(0.0)

        def _mean(xs):
            return round(sum(xs) / len(xs), 4) if xs else 0.0

        is_avg_all  = _mean(is_rs)
        oos_avg_all = _mean(oos_rs)
        score = round(oos_avg_all - overfit_penalty * abs(is_avg_all - oos_avg_all), 4)

        return {
            "params":   kw,
            "is_avg_r": is_avg_all,
            "oos_avg_r": oos_avg_all,
            "score":    score,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(_eval, combos))

    results.sort(key=lambda r: r["score"], reverse=True)

    best = results[0] if results else None
    if log and best:
        experiments.log_run(
            "optimize", symbols, f"{start}~{end}",
            {**best["params"], "_grid": {k: grid[k] for k in keys}},
            {"oos_avg_r": best["oos_avg_r"], "is_avg_r": best["is_avg_r"],
             "score": best["score"]},
        )

    return {"keys": keys, "tested": len(combos), "results": results[:50], "best": best}
