"""连接健康 / 出口IP体检。"""
import asyncio
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db, Settings
from ..proxy import rewrite_proxy_for_runtime
from ..state import app_state
from ..services.bot_engine import bot_engine

router = APIRouter()

# Binance 受限地区（合约不可用）——出口落这些国家会被 geo 封
_RESTRICTED = {"US", "United States", "CN", "China"}


@router.get("")
async def health(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    proxy = s.proxy_url if s else None
    info = {"connected": app_state.client is not None,
            "engine_running": bot_engine.running,
            "execution_mode": "exchange",
            "testnet": s.testnet if s else None,
            "proxy_set": bool(proxy)}
    # 出口 IP + 国家
    try:
        import requests as rq
        runtime_proxy = rewrite_proxy_for_runtime(proxy) if proxy else None
        proxies = (
            {"http": runtime_proxy, "https": runtime_proxy}
            if runtime_proxy
            else None
        )
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
