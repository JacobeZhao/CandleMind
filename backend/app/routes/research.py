"""研究/稳健性接口：一键稳健性报告 + 实验登记表查询 + HTML 回测报告。"""
import asyncio
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session

from ..database import get_db, Strategy
from ..services import robustness, experiments, optimizer, backtest_portfolio, attribution, features

# 扩充后的候选币池（流动性较好的 USDT 永续）
UNIVERSE = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "DOGEUSDT",
            "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT",
            "UNIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "NEARUSDT", "FILUSDT", "INJUSDT",
            "SUIUSDT", "TIAUSDT", "AAVEUSDT", "ETCUSDT", "XLMUSDT", "SEIUSDT", "RUNEUSDT"]

router = APIRouter()


def _ml_strategy(db):
    strat = db.query(Strategy).filter(Strategy.strategy_type == "ml_trend").first()
    if not strat:
        raise HTTPException(404, "策略不存在")
    return strat


class RobustnessReq(BaseModel):
    symbols:      List[str] = ["BTCUSDT", "BNBUSDT", "XRPUSDT"]
    start_date:   str = "2025-01-01"
    end_date:     str = "2026-06-05"
    risk_pct:     Optional[float] = None
    split_date:   Optional[str] = None
    window_months: int = 3


@router.post("/robustness")
async def run_robustness(req: RobustnessReq, db: Session = Depends(get_db)):
    strat = _ml_strategy(db)
    risk = req.risk_pct if req.risk_pct is not None else strat.risk_pct
    try:
        report = await asyncio.to_thread(
            robustness.full_report, req.symbols,
            req.start_date, req.end_date, risk, req.split_date, req.window_months)
        return report
    except Exception as e:
        raise HTTPException(500, str(e))


class OptimizeReq(BaseModel):
    symbols:    List[str] = ["BTCUSDT", "BNBUSDT", "XRPUSDT"]
    start_date: str = "2025-01-01"
    end_date:   str = "2026-06-05"
    split_date: str = "2026-01-01"
    grid: dict = {"exit_threshold": [0.30, 0.35, 0.40], "atr_trail": [2.5, 3.0, 4.0]}
    risk_pct:   Optional[float] = None


@router.post("/optimize")
async def optimize(req: OptimizeReq, db: Session = Depends(get_db)):
    strat = _ml_strategy(db)
    risk = req.risk_pct if req.risk_pct is not None else strat.risk_pct
    return await asyncio.to_thread(
        optimizer.grid_search, req.symbols, {}, req.grid,
        req.start_date, req.end_date, req.split_date, risk)


class PortfolioReq(BaseModel):
    symbols:    List[str] = ["BTCUSDT", "BNBUSDT", "XRPUSDT"]
    start_date: str = "2025-01-01"
    end_date:   str = "2026-06-05"
    capital:    float = 10000
    risk_pct:   Optional[float] = None
    alloc_mode: str = "invvol"   # equal | invvol(反比波动率)


@router.post("/portfolio")
async def portfolio(req: PortfolioReq, db: Session = Depends(get_db)):
    strat = _ml_strategy(db)
    risk = req.risk_pct if req.risk_pct is not None else strat.risk_pct
    return await asyncio.to_thread(
        backtest_portfolio.run_portfolio_backtest, req.symbols,
        req.start_date, req.end_date, req.capital, risk, None, req.alloc_mode)


class AttributionReq(BaseModel):
    symbols:    List[str] = ["BTCUSDT", "BNBUSDT", "XRPUSDT", "ETHUSDT", "SOLUSDT"]
    start_date: str = "2025-01-01"
    end_date:   str = "2026-06-05"
    risk_pct:   Optional[float] = None


@router.post("/attribution")
async def run_attribution(req: AttributionReq, db: Session = Depends(get_db)):
    strat = _ml_strategy(db)
    risk = req.risk_pct if req.risk_pct is not None else strat.risk_pct
    return await asyncio.to_thread(
        attribution.attribution_report, req.symbols, req.start_date, req.end_date, risk)


class FeaturesReq(BaseModel):
    symbols:      Optional[List[str]] = None
    end:          str = "2026-06-05"
    lookback_days: int = 180


@router.post("/features/build")
async def build_features(req: FeaturesReq):
    syms = req.symbols or UNIVERSE
    fdf = await asyncio.to_thread(features.build_feature_store, syms, req.end, req.lookback_days, True)
    stab = await asyncio.to_thread(features.stability_check, syms)
    return {"features": fdf.to_dict(orient="records"), "stability": stab, "stable_set": sorted(features.STABLE)}


@router.get("/features")
def get_features():
    fdf = features.load_latest()
    return {"features": fdf.to_dict(orient="records") if not fdf.empty else [],
            "stable_set": sorted(features.STABLE)}


@router.get("/reports")
def list_reports():
    from ..services.html_report import HTML_DIR
    files = sorted(HTML_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1),
             "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
            for p in files]


def _resolve_report_path(report_dir: Path, name: str) -> Path:
    requested = Path(name)
    if requested.name != name or requested.suffix.lower() != ".html":
        raise FileNotFoundError(name)
    root = report_dir.resolve(strict=True)
    candidate = (root / requested.name).resolve(strict=True)
    if candidate.parent != root or not candidate.is_file():
        raise FileNotFoundError(name)
    return candidate


@router.get("/report/{name}", response_class=HTMLResponse)
def get_report(name: str):
    from ..services.html_report import HTML_DIR
    try:
        p = _resolve_report_path(HTML_DIR, name)
    except (FileNotFoundError, OSError, RuntimeError):
        raise HTTPException(404, "Report not found")
    if ".." in name or not p.exists():
        raise HTTPException(404, "报告不存在")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/experiments")
def list_experiments(limit: int = 100, kind: Optional[str] = None):
    return experiments.list_runs(limit, kind)


@router.get("/leaderboard")
def leaderboard(metric: str = "oos_pct", limit: int = 20):
    return experiments.leaderboard(metric, limit)


@router.get("/experiments/{rid}")
def get_experiment(rid: int):
    r = experiments.get_run(rid)
    if not r:
        raise HTTPException(404, "记录不存在")
    return r


# ── ML 信号 ───────────────────────────────────────────────────────────────────

@router.get("/ml/thresholds")
def get_ml_thresholds():
    """返回 Phase 5 生成的每币阈值表。未生成则返回空 dict。"""
    try:
        from ..services.ml_signal import _load_thresholds
        return _load_thresholds()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/ml/signal/{symbol}")
async def get_ml_signal_route(
    symbol: str,
    mode: str = "recommended",   # recommended | thresh_f1 | thresh_p65
):
    """实时查询单币 ML 信号（加载已训练模型 + 最近 30 天特征）。"""
    try:
        from ..services.ml_signal import get_ml_signal
        sig = await asyncio.to_thread(
            get_ml_signal, symbol.upper(), None, None, 0.05, 30, True, mode
        )
        return {
            "symbol":         sig.symbol,
            "long_prob":      sig.long_prob,
            "short_prob":     sig.short_prob,
            "action":         sig.action,
            "confidence":     sig.confidence,
            "pos_size_mult":  sig.pos_size_mult,
            "threshold_used": sig.threshold_used,
            "drift_warning":  sig.drift_warning,
            "model_available":sig.model_available,
            "feature_timestamp": sig.feature_timestamp,
            "data_age_seconds": sig.data_age_seconds,
            "feature_fresh":  sig.feature_fresh,
            "note":           sig.note,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/ml/scan")
async def ml_scan(symbols: str = "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,SOLUSDT"):
    """批量扫描多个币种的 ML 信号，返回有效信号（非 observe）列表。"""
    try:
        from ..services.ml_signal import scan_universe
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        results  = await asyncio.to_thread(scan_universe, sym_list)
        return [
            {
                "symbol":         r.symbol,
                "action":         r.action,
                "long_prob":      r.long_prob,
                "short_prob":     r.short_prob,
                "confidence":     r.confidence,
                "pos_size_mult":  r.pos_size_mult,
                "drift_warning":  r.drift_warning,
                "feature_timestamp": r.feature_timestamp,
                "data_age_seconds": r.data_age_seconds,
                "feature_fresh":  r.feature_fresh,
                "note":           r.note,
            }
            for r in results
        ]
    except Exception as e:
        raise HTTPException(500, str(e))


class MLBacktestReq(BaseModel):
    symbol:                str   = "BTCUSDT"
    start_date:            str   = "2024-01-01"
    end_date:              str   = "2026-06-09"
    exit_threshold:        float = 0.35
    reversal_threshold:    float = 0.58
    atr_trail:             float = 3.0
    max_adds:              int   = 3
    add_atr_dist:          float = 0.8
    kelly_frac:            float = 0.25
    risk_pct:              float = 0.01


@router.post("/ml/backtest")
async def ml_backtest(req: MLBacktestReq):
    """
    运行 Pure-ML 趋势策略回测。
    入场阈值自动从 thresholds.json 读取（按币种校准）。
    """
    try:
        from ..services.ml_strategy import MLTrendParams, backtest_ml_trend
        params = MLTrendParams.from_thresholds(
            req.symbol.upper(),
            exit_threshold     = req.exit_threshold,
            reversal_threshold = req.reversal_threshold,
            atr_trail          = req.atr_trail,
            max_adds           = req.max_adds,
            add_atr_dist       = req.add_atr_dist,
            kelly_frac         = req.kelly_frac,
            risk_pct           = req.risk_pct,
        )
        result = await asyncio.to_thread(
            backtest_ml_trend,
            req.symbol.upper(), params,
            req.start_date, req.end_date,
            False,  # verbose=False（不在服务器打印）
        )
        if "error" in result:
            raise HTTPException(400, result["error"])

        # TradeRecord → serializable dict
        trades_out = [
            {
                "entry_time":    t.entry_time,
                "exit_time":     t.exit_time,
                "direction":     t.direction,
                "entry_price":   t.entry_price,
                "exit_price":    t.exit_price,
                "avg_price":     t.avg_price,
                "pnl_r":         t.pnl_r,
                "reason":        t.reason,
                "adds":          t.adds,
                "entry_prob":    t.entry_prob,
                "exit_prob":     t.exit_prob,
                "duration_bars": t.duration_bars,
            }
            for t in result.get("trades", [])
        ]
        return {
            "symbol":  result["symbol"],
            "metrics": result["metrics"],
            "params": {
                "entry_long_threshold":  params.entry_long_threshold,
                "entry_short_threshold": params.entry_short_threshold,
                "exit_threshold":        params.exit_threshold,
                "reversal_threshold":    params.reversal_threshold,
                "atr_trail":             params.atr_trail,
                "max_adds":              params.max_adds,
            },
            "trades":  trades_out,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
