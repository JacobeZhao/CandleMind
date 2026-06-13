import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio
import pandas as pd

from ..state import app_state
from ..services.backtest import fetch_historical, run_backtest, cache_path
from ..services.ml_strategy import backtest_ml_trend, MLTrendParams
from ..database import get_db, Strategy
from ..datastore import BACKTEST_DIR
from fastapi import Depends
from sqlalchemy.orm import Session

router = APIRouter()


def _save_backtest(req, symbol: str, s_type: str, result: dict) -> None:
    """把每次回测（请求参数 + 完整结果）存到 G:\\5、金融交易\\backtests。"""
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = BACKTEST_DIR / f"{ts}_{symbol}_{s_type}.json"
        fn.write_text(json.dumps({
            "saved_at": ts, "symbol": symbol, "strategy_type": s_type,
            "request": req.model_dump(), "result": result,
        }, ensure_ascii=False), encoding="utf-8")
        # 登记到实验表（含样本外）
        from ..services import experiments
        m = dict(result.get("metrics", {}))
        wf = result.get("walk_forward")
        if wf:
            m["oos_pct"] = wf.get("out_sample", {}).get("total_return_pct")
        from ..services.datafeed import data_signature
        experiments.log_run("backtest", symbol, f"{req.start_date}~{req.end_date}",
                            req.model_dump(), m, str(fn), data_hash=data_signature([symbol]))
        # 生成可在 web 端查看的 HTML 报告
        from ..services.html_report import save_report
        save_report({"symbol": symbol, "strategy_type": s_type,
                     "period": f"{req.start_date}~{req.end_date}",
                     "initial_capital": req.initial_capital, "risk_pct": req.risk_pct,
                     "exec_mode": getattr(req, "exec_mode", "market")}, result)
    except Exception:
        pass   # 存档失败不影响回测返回


def _midpoint(start: str, end: str) -> str:
    """区间中点日期（YYYY-MM-DD），作为样本外默认切分点。"""
    from datetime import datetime, timedelta
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    return (s + (e - s) / 2).strftime("%Y-%m-%d")


def _ml_result_to_frontend(ml_result: dict, capital: float, risk_pct: float) -> dict:
    """
    Convert backtest_ml_trend() raw result to the frontend-compatible format
    expected by Backtest.jsx.

    ml_result keys: symbol, params, metrics (ML calc_metrics dict), trades (list[TradeRecord])
    """
    m = ml_result.get("metrics", {})
    raw_trades = ml_result.get("trades", [])

    n_trades   = m.get("n_trades", 0)
    total_r    = m.get("total_r", 0.0)
    max_dd_r   = m.get("max_dd_r", 0.0)
    sharpe     = m.get("sharpe", 0.0)
    win_rate   = m.get("win_rate", 0.0)
    avg_r      = m.get("avg_r", 0.0)

    total_return_pct = total_r * risk_pct * 100
    max_drawdown     = abs(max_dd_r) * risk_pct * 100
    avg_pnl          = avg_r * risk_pct * capital

    total_adds = sum(getattr(t, "adds", 0) for t in raw_trades)

    frontend_metrics = {
        "total_return_pct": round(total_return_pct, 4),
        "gross_return_pct": round(total_return_pct, 4),
        "max_drawdown":     round(max_drawdown, 4),
        "sharpe":           round(sharpe, 4),
        "win_rate":         round(win_rate * 100, 2),
        "num_trades":       n_trades,
        "total_adds":       total_adds,
        "total_fees":       0.0,      # already embedded in pnl_r
        "slippage_cost":    0.0,      # not modelled separately
        "total_funding":    0.0,      # not modelled
        "avg_pnl":          round(avg_pnl, 4),
    }

    # Build equity curve (starts at initial_capital, adds pnl per trade sorted by exit_time)
    sorted_trades = sorted(raw_trades, key=lambda t: int(t.exit_time))
    equity = capital
    equity_curve = [{"time": pd.Timestamp(
        sorted_trades[0].entry_time if sorted_trades else 0, unit="ms"
    ).isoformat(), "equity": round(equity, 4)}] if sorted_trades else []

    for t in sorted_trades:
        pnl = t.pnl_r * capital * risk_pct
        equity += pnl
        equity_curve.append({
            "time":   pd.Timestamp(int(t.exit_time), unit="ms").isoformat(),
            "equity": round(equity, 4),
        })

    # Build trades list
    frontend_trades = []
    for t in sorted_trades:
        pnl     = t.pnl_r * capital * risk_pct
        pnl_pct = t.pnl_r * risk_pct * 100
        frontend_trades.append({
            "entry_time":  pd.Timestamp(int(t.entry_time), unit="ms").isoformat(),
            "exit_time":   pd.Timestamp(int(t.exit_time),  unit="ms").isoformat(),
            "side":        "LONG" if t.direction == 1 else "SHORT",
            "mode":        "ml_trend",
            "adds":        t.adds,
            "entry_price": t.entry_price,
            "exit_price":  t.exit_price,
            "pnl":         round(pnl, 4),
            "pnl_pct":     round(pnl_pct, 4),
            "exit_reason": t.reason,
        })

    return {
        "metrics":      frontend_metrics,
        "equity_curve": equity_curve,
        "trades":       frontend_trades,
    }


def _wf_metrics_from_trades(trades: list, equity_curve: list,
                             split_ts_ms: int, capital: float) -> dict:
    """
    Split frontend-format trades by split_ts_ms (millisecond epoch) and
    compute IS / OOS summary metrics identical to what split_metrics() in
    backtest_mtf returns.
    """
    def _subset_metrics(subset):
        if not subset:
            return {"total_return_pct": 0, "num_trades": 0,
                    "win_rate": 0, "sharpe": 0, "max_drawdown": 0}
        pnls = [t["pnl_pct"] for t in subset]
        wins = sum(1 for p in pnls if p > 0)
        total_ret = sum(pnls)
        win_rate  = wins / len(pnls) * 100
        import numpy as np
        arr = np.array(pnls)
        sharpe = float(arr.mean() / (arr.std() + 1e-9))
        # max drawdown from running equity
        eq = capital
        peak = capital
        max_dd = 0.0
        for t in subset:
            eq += t["pnl"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        return {
            "total_return_pct": round(total_ret, 4),
            "num_trades":       len(subset),
            "win_rate":         round(win_rate, 2),
            "sharpe":           round(sharpe, 4),
            "max_drawdown":     round(max_dd, 4),
        }

    def _ts_ms(trade):
        return int(pd.Timestamp(trade["exit_time"]).value // 1_000_000)

    is_trades  = [t for t in trades if _ts_ms(t) <= split_ts_ms]
    oos_trades = [t for t in trades if _ts_ms(t) >  split_ts_ms]

    return {
        "in_sample":  _subset_metrics(is_trades),
        "out_sample": _subset_metrics(oos_trades),
        "split_ts":   split_ts_ms,
    }


class RunRequest(BaseModel):
    # Either pick a saved strategy by id, or specify inline
    strategy_id:     Optional[int]  = None
    symbol:          Optional[str]  = None   # None = use strategy default
    interval:        str            = "1h"
    start_date:      str            = ""
    end_date:        str            = ""
    strategy_type:   str            = "supertrend"
    strategy_params: dict           = {}
    # Override risk params (falls back to strategy's own values)
    initial_capital: float          = 10000
    leverage:        Optional[int]  = None
    risk_pct:        Optional[float]= None
    sl_pct:          Optional[float]= None
    tp_pct:          Optional[float]= None
    # —— 成本模型 ——
    taker_fee:       float          = 0.0004  # taker 手续费率
    slippage_bps:    float          = 5.0     # 逆向滑点（基点）
    funding_rate:    float          = 0.0001  # 资金费率 / 8h
    use_funding:     bool           = True
    fee_mult:        float          = 1.0     # 手续费压力测试倍数
    # —— 样本外验证 ——
    walk_forward:    bool           = False
    split_date:      Optional[str]  = None    # 缺省取区间中点
    # —— 执行模型 ——
    exec_mode:       str            = "market"  # market | limit(maker)
    maker_fee:       float          = 0.0002
    model_liq:       bool           = False     # 是否建模爆仓（按策略杠杆）


@router.post("/run")
async def backtest_run(req: RunRequest, db: Session = Depends(get_db)):
    if not app_state.client:
        raise HTTPException(503, "未连接 Binance，请先配置 API Key")

    symbol   = req.symbol or "BTCUSDT"
    interval = req.interval
    s_type   = req.strategy_type
    s_params = req.strategy_params
    leverage = req.leverage or 1
    risk_pct = req.risk_pct or 0.01
    sl_pct   = req.sl_pct   or 0.015
    tp_pct   = req.tp_pct   or 0.03

    # Load from saved strategy if id provided
    if req.strategy_id:
        strat = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
        if not strat:
            raise HTTPException(404, "策略不存在")
        symbol   = req.symbol or strat.symbol   # req.symbol overrides if provided
        interval = strat.interval
        s_type   = strat.strategy_type
        s_params = json.loads(strat.strategy_params_json or "{}")
        leverage = req.leverage   or strat.leverage
        risk_pct = req.risk_pct   or strat.risk_pct
        sl_pct   = req.sl_pct     or strat.stop_loss_pct
        tp_pct   = req.tp_pct     or strat.take_profit_pct
        # AI strategy
        if strat.ai_strategy_json:
            ai = json.loads(strat.ai_strategy_json)
            s_type   = "conditions"
            s_params = {**ai.get("strategy", {}),
                        "indicators_needed": ai.get("indicators_needed", [])}

    if not req.start_date or not req.end_date:
        raise HTTPException(400, "请提供 start_date 和 end_date")

    try:
        if s_type == "ml_trend":
            # Build MLTrendParams from strategy_params (unknown keys are silently ignored)
            valid_fields = MLTrendParams.__dataclass_fields__.keys()
            filtered = {k: v for k, v in s_params.items() if k in valid_fields}
            # Always override risk_pct from request so Kelly sizing uses correct capital fraction
            filtered["risk_pct"] = risk_pct
            ml_params = MLTrendParams.from_thresholds(symbol, **filtered)

            ml_result = await asyncio.to_thread(
                backtest_ml_trend,
                symbol, ml_params,
                req.start_date, req.end_date,
                False,   # verbose=False for API calls
            )

            if "error" in ml_result:
                raise HTTPException(400, ml_result["error"])

            result = _ml_result_to_frontend(ml_result, req.initial_capital, risk_pct)

            if req.walk_forward:
                split_date = req.split_date or _midpoint(req.start_date, req.end_date)
                split_ts   = int(pd.Timestamp(split_date).value // 1_000_000)
                result["walk_forward"] = _wf_metrics_from_trades(
                    result["trades"], result["equity_curve"],
                    split_ts, req.initial_capital,
                )

            _save_backtest(req, symbol, s_type, result)
            return result

        # Generic fallback for supertrend and other strategy types
        df = await asyncio.to_thread(
            fetch_historical, app_state.client,
            symbol, interval, req.start_date, req.end_date,
        )
        if len(df) < 20:
            raise HTTPException(400, "数据量不足，请扩大时间范围")
        result = await asyncio.to_thread(
            run_backtest, df, s_type, s_params,
            req.initial_capital, leverage, risk_pct, sl_pct, tp_pct,
        )
        _save_backtest(req, symbol, s_type, result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/download")
async def download_data(symbol: str, interval: str, start_date: str, end_date: str):
    if not app_state.client:
        raise HTTPException(503, "未连接 Binance，请先配置 API Key")
    cp = cache_path(symbol, interval, start_date, end_date)
    if cp.exists():
        return {"ok": True, "cached": True, "message": "已有缓存，无需重新下载"}
    try:
        df = await asyncio.to_thread(
            fetch_historical, app_state.client,
            symbol, interval, start_date, end_date,
            0.25,   # 每批次 250ms 延迟
        )
        batches = max(1, len(df) // 1000)
        return {
            "ok": True, "cached": False, "bars": len(df),
            "message": f"已分 {batches} 批下载并缓存 {len(df)} 根 K 线",
        }
    except Exception as e:
        raise HTTPException(500, str(e))
