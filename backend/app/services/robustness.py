"""
稳健性套件：walk-forward / 多品种 / 蒙特卡洛 / 参数敏感性 / 一键完整报告。
全部复用 ml_strategy.backtest_ml_trend + calc_metrics。
报告存 MARKET_ROOT/reports/，并登记到实验表。
所有指标均为 R 单位（avg_r, total_r, max_dd_r），不使用 %-based 指标。
"""
import json
from datetime import datetime
from calendar import monthrange
from dataclasses import asdict

import numpy as np

from .ml_strategy import MLTrendParams, backtest_ml_trend, calc_metrics
from . import experiments
from ..datastore import REPORTS_DIR

# REPORTS_DIR is provided by datastore
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
INTERMEDIATE_DIR = REPORTS_DIR / "intermediate"
INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)


def _add_months(d: datetime, n: int) -> datetime:
    m = d.month - 1 + n
    y = d.year + m // 12
    mo = m % 12 + 1
    return d.replace(year=y, month=mo, day=min(d.day, monthrange(y, mo)[1]))


def _windows(start: str, end: str, months: int) -> list:
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    out = []
    cur = s
    while cur < e:
        nxt = min(_add_months(cur, months), e)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def _date_to_ms(date_str: str) -> int:
    """Convert 'YYYY-MM-DD' string to milliseconds timestamp (UTC midnight)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


# ── 1. walk-forward（固定参数下的滚动 OOS 一致性）─────────────────────────────
def walk_forward(symbol, params=None, start="2024-01-01", end="2026-06-09",
                 window_months=3):
    """
    Run backtest_ml_trend over full range, then slice trades per rolling window.
    Returns per-window R-based metrics and overall consistency stats.
    """
    if params is None:
        params = MLTrendParams.from_thresholds(symbol)

    d = backtest_ml_trend(symbol, params, start=start, end=end, verbose=False)
    if "error" in d:
        return {"symbol": symbol, "error": d["error"]}

    all_trades = d["trades"]
    wins = []
    for ws, we in _windows(start, end, window_months):
        ws_ms = _date_to_ms(ws)
        we_ms = _date_to_ms(we)
        window_trades = [
            t for t in all_trades
            if int(t.exit_time) > ws_ms and int(t.exit_time) < we_ms
        ]
        m = calc_metrics(window_trades)
        wins.append({
            "window": f"{ws}~{we}",
            "n_trades": m.get("n_trades", 0),
            "win_rate": m.get("win_rate", 0.0),
            "avg_r": m.get("avg_r", 0.0),
            "total_r": m.get("total_r", 0.0),
            "max_dd_r": m.get("max_dd_r", 0.0),
            "sharpe": m.get("sharpe", 0.0),
        })

    pos = sum(1 for w in wins if w["total_r"] > 0)
    return {
        "symbol": symbol,
        "windows": wins,
        "consistency_pct": round(pos / len(wins) * 100, 1) if wins else 0,
        "positive": pos,
        "total": len(wins),
    }


# ── 2. 多品种 ────────────────────────────────────────────────────────────────
def multi_symbol(symbols, params=None, start="2024-01-01", end="2026-06-09",
                 split_date=None):
    """
    Run backtest_ml_trend per symbol. Optionally split trades at split_date for
    in-sample / out-of-sample comparison.
    Returns rows with R-based metrics and a summary dict.
    """
    rows = []
    split_ts = _date_to_ms(split_date) if split_date else None

    for s in symbols:
        p = params if params is not None else MLTrendParams.from_thresholds(s)
        d = backtest_ml_trend(s, p, start=start, end=end, verbose=False)
        if "error" in d:
            rows.append({"symbol": s, "error": d["error"]})
            continue

        m = d["metrics"]
        trades = d["trades"]
        row = {
            "symbol": s,
            "net_r": m.get("total_r", 0.0),
            "win_rate": m.get("win_rate", 0.0),
            "n_trades": m.get("n_trades", 0),
            "max_dd_r": m.get("max_dd_r", 0.0),
            "sharpe": m.get("sharpe", 0.0),
            "avg_r": m.get("avg_r", 0.0),
            "profit_factor": m.get("profit_factor", 0.0),
        }

        if split_ts is not None:
            is_trades = [t for t in trades if int(t.exit_time) < split_ts]
            oos_trades = [t for t in trades if int(t.exit_time) >= split_ts]
            is_m = calc_metrics(is_trades)
            oos_m = calc_metrics(oos_trades)
            row["is_total_r"] = is_m.get("total_r", 0.0)
            row["oos_total_r"] = oos_m.get("total_r", 0.0)
            row["is_n_trades"] = is_m.get("n_trades", 0)
            row["oos_n_trades"] = oos_m.get("n_trades", 0)

        rows.append(row)

    valid_rows = [r for r in rows if "error" not in r]
    if not valid_rows:
        return {"rows": rows, "summary": {"error": "no valid symbols"}}

    nets = [r["net_r"] for r in valid_rows]
    summary = {
        "avg_net_r": round(sum(nets) / len(nets), 3),
        "positive": sum(1 for x in nets if x > 0),
        "total": len(nets),
        "worst_r": round(min(nets), 3),
        "best_r": round(max(nets), 3),
    }
    if split_ts is not None:
        oos_nets = [r["oos_total_r"] for r in valid_rows]
        summary["avg_oos_r"] = round(sum(oos_nets) / len(oos_nets), 3)
        summary["oos_positive"] = sum(1 for x in oos_nets if x > 0)

    return {"rows": rows, "summary": summary}


# ── 3. 蒙特卡洛（自助重采样逐笔 pnl_r → 回撤分布 + 破产概率）────────────────
def monte_carlo(trades, n=1000, ruin_r=-20.0, seed=42):
    """
    Bootstrap resample pnl_r array from trades list.
    Each TradeRecord has a .pnl_r attribute.
    Returns R-based distribution stats and risk-of-ruin.
    ruin_r: cumulative R level considered 'ruin' (default -20R).
    """
    pnls = np.array([t.pnl_r for t in trades], dtype=float)
    if len(pnls) < 5:
        return {"error": "交易样本太少"}

    rng = np.random.default_rng(seed)
    k = len(pnls)
    finals, dds, ruined = [], [], 0

    for _ in range(n):
        sample = rng.choice(pnls, size=k, replace=True)
        path = np.cumsum(sample)
        finals.append(float(path[-1]))
        full = np.concatenate([[0.0], path])
        peak = np.maximum.accumulate(full)
        dd = float((peak - full).max())
        dds.append(dd)
        if path.min() <= ruin_r:
            ruined += 1

    finals = np.array(finals)
    dds = np.array(dds)
    pct = lambda a, q: round(float(np.percentile(a, q)), 3)

    return {
        "sims": n,
        "total_r_median": pct(finals, 50),
        "total_r_p05": pct(finals, 5),
        "total_r_p95": pct(finals, 95),
        "maxdd_r_median": pct(dds, 50),
        "maxdd_r_p95": pct(dds, 95),
        "risk_of_ruin_pct": round(ruined / n * 100, 2),
    }


# ── 4. 参数敏感性 ────────────────────────────────────────────────────────────
def sensitivity(symbol, params=None, name="atr_trail", values=None,
                start="2024-01-01", end="2026-06-09", split_date=None):
    """
    Vary one MLTrendParams field across given values, run backtest_ml_trend
    per value, return R-based curve.
    The named param is passed as a kwarg to MLTrendParams.from_thresholds.
    """
    if values is None:
        values = [2.0, 3.0, 4.0, 5.0, 6.0]

    split_ts = _date_to_ms(split_date) if split_date else None
    curve = []

    for v in values:
        # Build params with the varied field overriding via from_thresholds kwargs
        p = MLTrendParams.from_thresholds(symbol, **{name: v})
        d = backtest_ml_trend(symbol, p, start=start, end=end, verbose=False)
        if "error" in d:
            curve.append({"value": v, "error": d["error"]})
            continue

        m = d["metrics"]
        pt = {
            "value": v,
            "total_r": m.get("total_r", 0.0),
            "avg_r": m.get("avg_r", 0.0),
            "win_rate": m.get("win_rate", 0.0),
            "n_trades": m.get("n_trades", 0),
            "max_dd_r": m.get("max_dd_r", 0.0),
            "sharpe": m.get("sharpe", 0.0),
        }

        if split_ts is not None:
            trades = d["trades"]
            oos_trades = [t for t in trades if int(t.exit_time) >= split_ts]
            oos_m = calc_metrics(oos_trades)
            pt["oos_total_r"] = oos_m.get("total_r", 0.0)
            pt["oos_n_trades"] = oos_m.get("n_trades", 0)

        curve.append(pt)

    return {"param": name, "symbol": symbol, "curve": curve}


# ── 5. 一键完整报告 ──────────────────────────────────────────────────────────
def full_report(symbols, params=None, start="2024-01-01", end="2026-06-09",
                split_date=None, wf_window=3,
                sens_param="atr_trail", sens_values=None) -> dict:
    """
    Combine multi_symbol + walk_forward + monte_carlo + sensitivity into one
    report dict, save JSON to REPORTS_DIR, log to experiments table.
    All metrics are R-based.
    """
    if split_date is None:
        mid_windows = _windows(start, end, 1)
        split_date = mid_windows[len(mid_windows) // 2][0]

    ms = multi_symbol(symbols, params, start, end, split_date=split_date)
    wf = [walk_forward(s, params, start, end, wf_window) for s in symbols]

    # Per-symbol intermediate results saved to disk
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    inter_dir = INTERMEDIATE_DIR / ts
    inter_dir.mkdir(parents=True, exist_ok=True)

    all_trades = []
    for s in symbols:
        p = params if params is not None else MLTrendParams.from_thresholds(s)
        d = backtest_ml_trend(s, p, start=start, end=end, verbose=False)
        sym_trades = d.get("trades", [])
        all_trades.extend(sym_trades)

        # Serialize params safely
        try:
            params_dict = asdict(p) if hasattr(p, "__dataclass_fields__") else vars(p)
        except Exception:
            params_dict = str(p)

        # Serialize metrics and trades (trades not JSON-serialisable directly)
        sym_metrics = d.get("metrics", {})
        trades_serialized = [
            {
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "direction": t.direction,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl_r": t.pnl_r,
                "reason": t.reason,
                "adds": t.adds,
            }
            for t in sym_trades
        ]
        (inter_dir / f"{s}.json").write_text(
            json.dumps(
                {
                    "symbol": s,
                    "params": params_dict,
                    "period": f"{start}~{end}",
                    "metrics": sym_metrics,
                    "trades": trades_serialized,
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

    mc = monte_carlo(all_trades) if all_trades else {"error": "no trades"}
    sens = sensitivity(
        symbols[0], params,
        name=sens_param,
        values=sens_values or [2.0, 3.0, 4.0, 5.0, 6.0],
        start=start,
        end=end,
        split_date=split_date,
    )

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols,
        "period": f"{start}~{end}",
        "split_date": split_date,
        "intermediate_dir": str(inter_dir),
        "multi_symbol": ms,
        "walk_forward": wf,
        "monte_carlo": mc,
        "sensitivity": sens,
    }

    # Save report JSON
    path = REPORTS_DIR / f"{ts}_robustness.json"
    path.write_text(json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")

    # Build summary metrics for experiment log (R-based)
    summary_metrics = {
        "avg_net_r": ms["summary"].get("avg_net_r"),
        "avg_oos_r": ms["summary"].get("avg_oos_r"),
        "maxdd_r_median": mc.get("maxdd_r_median"),
        "n_trades": sum(
            r.get("n_trades", 0) for r in ms["rows"] if "error" not in r
        ),
    }

    # Build params dict for logging
    try:
        if params is not None:
            log_params = asdict(params) if hasattr(params, "__dataclass_fields__") else vars(params)
        else:
            log_params = {"source": "per_symbol_from_thresholds"}
    except Exception:
        log_params = str(params)

    from .datafeed import data_signature
    experiments.log_run(
        "robustness",
        symbols,
        f"{start}~{end}",
        log_params,
        summary_metrics,
        str(path),
        data_hash=data_signature(symbols),
    )

    return report
