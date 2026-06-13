"""
归因分析：把 ML 趋势策略回测逐笔按方向/离场原因/加仓档/月份/入场概率分桶分解，
每桶算 笔数/总R/平均R/胜率/盈利因子。用于定位收益与亏损来源。
报告存 MARKET_ROOT/reports/attribution/，并登记实验。
"""
import json
import pandas as pd
from datetime import datetime

from .ml_strategy import MLTrendParams, backtest_ml_trend
from . import experiments
from ..datastore import MARKET_ROOT

ATTR_DIR = MARKET_ROOT / "reports" / "attribution"
ATTR_DIR.mkdir(parents=True, exist_ok=True)


def _stats(trades) -> dict:
    """Compute bucket statistics over a list of TradeRecord objects."""
    n = len(trades)
    if n == 0:
        return {"trades": 0, "total_r": 0.0, "avg_r": 0.0, "win_rate": 0.0, "profit_factor": None}
    wins = [t.pnl_r for t in trades if t.pnl_r > 0]
    losses = [t.pnl_r for t in trades if t.pnl_r <= 0]
    sw = sum(wins)
    sl = sum(losses)
    total_r = sw + sl
    return {
        "trades": n,
        "total_r": round(total_r, 4),
        "avg_r": round(total_r / n, 4),
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": round(sw / abs(sl), 4) if sl else None,
    }


def _group(trades, keyfn) -> dict:
    buckets: dict = {}
    for t in trades:
        buckets.setdefault(keyfn(t), []).append(t)
    return {str(k): _stats(v) for k, v in sorted(buckets.items(), key=lambda x: str(x[0]))}


def _entry_prob_bucket(prob: float) -> str:
    if prob < 0.52:
        return "<0.52"
    if prob < 0.55:
        return "0.52-0.55"
    if prob < 0.60:
        return "0.55-0.60"
    return ">=0.60"


def attribute(trades) -> dict:
    """Multi-dimensional attribution over a list of TradeRecord objects."""
    return {
        "overall": _stats(trades),
        "by_direction": _group(
            trades,
            lambda t: "LONG" if t.direction == 1 else "SHORT",
        ),
        "by_reason": _group(
            [t for t in trades if t.reason in ("stop", "ml_exit", "ml_reversal")],
            lambda t: t.reason,
        ),
        "by_adds": _group(trades, lambda t: t.adds),
        "by_month": _group(
            trades,
            lambda t: pd.Timestamp(t.exit_time, unit="ms").strftime("%Y-%m"),
        ),
        "by_entry_prob": _group(trades, lambda t: _entry_prob_bucket(t.entry_prob)),
    }


def attribution_report(symbols, start, end, risk_pct=0.01) -> dict:
    """Run multi-symbol ML-trend backtest, attribute all trades, save report and log experiment."""
    all_trades = []
    for symbol in symbols:
        result = backtest_ml_trend(symbol, params=None, start=start, end=end, verbose=False)
        if "error" in result:
            continue
        all_trades.extend(result["trades"])

    attr = attribute(all_trades)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols,
        "period": f"{start}~{end}",
        "attribution": attr,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = ATTR_DIR / f"{ts}_attribution.json"
    path.write_text(json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")

    ov = attr["overall"]
    experiments.log_run(
        "attribution",
        symbols,
        f"{start}~{end}",
        {},
        {
            "total_r": ov["total_r"],
            "num_trades": ov["trades"],
            "profit_factor": ov["profit_factor"],
        },
        str(path),
    )
    return report
