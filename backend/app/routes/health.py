"""Runtime health and advisory exit-location diagnostics."""
import asyncio
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db, Settings
from ..proxy import rewrite_proxy_for_runtime
from ..state import app_state
from ..services.bot_engine import bot_engine
from ..services.exchange_provider import BINANCE_PROVIDER, normalize_exchange_provider

router = APIRouter()

@router.get("")
async def health(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    proxy = s.proxy_url if s else None
    provider = normalize_exchange_provider(
        getattr(s, "exchange_provider", None) if s else None
    )
    info = {
        "connected": (
            provider == BINANCE_PROVIDER
            and app_state.exchange_provider == BINANCE_PROVIDER
            and app_state.client is not None
        ),
        "provider": provider,
        "engine_running": bot_engine.running,
        "execution_mode": "exchange",
        "testnet": s.testnet if s else None,
        "proxy_set": bool(proxy),
        "restricted": None,
    }
    # Third-party IP geolocation is advisory and cannot establish Binance access.
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
        info["location_advisory"] = r.get("countryCode") or None
    except asyncio.CancelledError:
        raise
    except Exception:
        info["exit_ip"] = None
        info["country"] = None
        info["ip_error"] = "Exit IP lookup failed"
    return info
