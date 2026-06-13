import json
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from ..database import get_db, Strategy
from ..state import app_state
from ..services.bot_engine import bot_engine

router = APIRouter()


class StrategyIn(BaseModel):
    name:                str
    description:         Optional[str] = ""
    symbol:              str   = "BTCUSDT"
    interval:            str   = "15m"
    leverage:            int   = 5
    risk_pct:            float = 0.01
    stop_loss_pct:       float = 0.015
    take_profit_pct:     float = 0.03
    strategy_type:       str   = "supertrend"
    strategy_params_json:Optional[str] = None
    ai_strategy_json:    Optional[str] = None


def _serialize(s: Strategy) -> dict:
    params = {}
    if s.strategy_params_json:
        try:
            params = json.loads(s.strategy_params_json)
        except Exception:
            pass
    return {
        "id":                 s.id,
        "name":               s.name,
        "description":        s.description or "",
        "symbol":             s.symbol,
        "interval":           s.interval,
        "leverage":           s.leverage,
        "risk_pct":           s.risk_pct,
        "stop_loss_pct":      s.stop_loss_pct,
        "take_profit_pct":    s.take_profit_pct,
        "strategy_type":      s.strategy_type,
        "strategy_params":    params,
        "strategy_params_json": s.strategy_params_json,
        "ai_strategy_json":   s.ai_strategy_json,
        "is_active":          s.is_active,
        "is_default":         s.is_default,
    }


# ── 引擎控制（静态路由必须在 /{id} 之前注册）────────────────────────────────────

@router.get("/engine/status")
def engine_status():
    return bot_engine.status


@router.post("/engine/start")
async def start_engine(paper: bool = False, db: Session = Depends(get_db)):
    if not app_state.client:
        raise HTTPException(503, "未连接 Binance")
    active = db.query(Strategy).filter(Strategy.is_active == True).first()
    if not active:
        raise HTTPException(400, "请先激活一个策略")
    cfg = {
        "name":            active.name,
        "symbol":          active.symbol,
        "interval":        active.interval,
        "leverage":        active.leverage,
        "risk_pct":        active.risk_pct,
        "stop_loss_pct":   active.stop_loss_pct,
        "take_profit_pct": active.take_profit_pct,
        "check_interval":  60,
        "strategy_type":   active.strategy_type,
        "strategy_params": json.loads(active.strategy_params_json or "{}"),
        "ai_strategy_json":active.ai_strategy_json,
        "paper":           paper,
        "initial_capital": 10000,
    }
    await bot_engine.start(app_state.client, cfg)
    mode = "纸面" if paper else "实盘"
    return {"ok": True, "message": f"策略「{active.name}」已启动（{mode}）"}


@router.post("/engine/stop")
async def stop_engine():
    await bot_engine.stop()
    return {"ok": True, "message": "已停止"}


# ── 静态路由 ──────────────────────────────────────────────────────────────────

@router.get("/list")
def list_strategies(db: Session = Depends(get_db)):
    strategies = db.query(Strategy).order_by(Strategy.is_default.desc(), Strategy.id).all()
    return [_serialize(s) for s in strategies]


@router.post("/create")
def create_strategy(body: StrategyIn, db: Session = Depends(get_db)):
    s = Strategy(
        name=body.name, description=body.description,
        symbol=body.symbol, interval=body.interval, leverage=body.leverage,
        risk_pct=body.risk_pct, stop_loss_pct=body.stop_loss_pct,
        take_profit_pct=body.take_profit_pct,
        strategy_type=body.strategy_type,
        strategy_params_json=body.strategy_params_json,
        ai_strategy_json=body.ai_strategy_json,
        is_default=False,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"ok": True, "id": s.id}


@router.post("/deactivate")
def deactivate_all(db: Session = Depends(get_db)):
    db.query(Strategy).update({Strategy.is_active: False})
    db.commit()
    return {"ok": True}


# ── 动态路由（/{strategy_id}）放最后 ─────────────────────────────────────────

@router.get("/{strategy_id}")
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "策略不存在")
    return _serialize(s)


@router.put("/{strategy_id}")
def update_strategy(strategy_id: int, body: StrategyIn, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "策略不存在")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "策略不存在")
    if s.is_default:
        raise HTTPException(400, "默认策略不可删除")
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/{strategy_id}/activate")
def activate_strategy(strategy_id: int, db: Session = Depends(get_db)):
    db.query(Strategy).update({Strategy.is_active: False})
    s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not s:
        raise HTTPException(404, "策略不存在")
    s.is_active = True
    db.commit()
    return {"ok": True}
