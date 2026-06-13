"""连接健康 / 出口IP体检 / 交易日志查询 / 实盘-回测对账。"""
import asyncio
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db, Settings
from ..state import app_state
from ..services.bot_engine import bot_engine
from ..services import journal

router = APIRouter()

# Binance 受限地区（合约不可用）——出口落这些国家会被 geo 封
_RESTRICTED = {"US", "United States", "CN", "China"}


@router.get("")
async def health(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    proxy = s.proxy_url if s else None
    info = {"connected": app_state.client is not None,
            "engine_running": bot_engine.running,
            "paper": bot_engine.paper,
            "testnet": s.testnet if s else None,
            "proxy_set": bool(proxy)}
    # 出口 IP + 国家
    try:
        import requests as rq
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = await asyncio.to_thread(
            lambda: rq.get("http://ip-api.com/json/?fields=query,countryCode,country",
                           proxies=proxies, timeout=10).json())
        info["exit_ip"] = r.get("query")
        info["country"] = r.get("country")
        info["restricted"] = r.get("countryCode") in _RESTRICTED
    except Exception as e:
        info["exit_ip"] = None; info["country"] = None; info["restricted"] = None
        info["ip_error"] = str(e)[:80]
    return info


@router.get("/journal")
def get_journal(limit: int = 200):
    return journal.read_recent(limit)


@router.get("/reconcile")
def reconcile(limit: int = 500):
    """对账：从交易日志统计纸面/实盘成交，给出与回测假设(滑点/手续费)对比的粗略口径。"""
    rows = journal.read_recent(limit)
    exits = [r for r in rows if r.get("type") in ("paper_exit", "live_exit")]
    entries = [r for r in rows if r.get("type") in ("paper_entry", "live_entry")]
    total_pnl = round(sum(r.get("pnl", 0) for r in exits), 2)
    wins = sum(1 for r in exits if r.get("pnl", 0) > 0)
    return {
        "entries": len(entries), "exits": len(exits),
        "total_pnl": total_pnl,
        "win_rate": round(wins / len(exits) * 100, 1) if exits else 0,
        "note": "对账口径：纸面/实盘逐笔已记入日志；积累足够实盘成交后可与回测的滑点假设(默认5bps)逐笔比对。",
        "recent": rows[:30],
    }
