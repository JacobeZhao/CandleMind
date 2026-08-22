from contextlib import asynccontextmanager
import asyncio
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .database import init_db, get_db, Settings, active_keys
from .state import app_state
from .binance_ws import binance_ws_client
from .ws_manager import manager
from .routes import (
    account,
    ai_config,
    health,
    market,
    market_agent,
    orders,
    settings,
    strategy_analytics,
)
from .routes import strategy as strategy_route
from .services.market_agent import market_agent_manager
from .services.market_agent_state_store import MarketAgentStateError
from .services.exchange_provider import BINANCE_PROVIDER, normalize_exchange_provider


async def _reconnect_loop():
    """连接健康守护：掉线且已配置 key 时自动重连（每 60s）。"""
    from .routes.settings import _connect_active
    while True:
        await asyncio.sleep(60)
        if app_state.client is None:
            try:
                db = next(get_db())
                try:
                    s = db.query(Settings).first()
                finally:
                    db.close()
                provider = normalize_exchange_provider(
                    getattr(s, "exchange_provider", None) if s else None
                )
                if s and provider == BINANCE_PROVIDER and active_keys(s)[0]:
                    await _connect_active(s)
                    logger.info("Auto-reconnect succeeded")
            except Exception:
                pass   # 仍连不上（如节点受限），下个周期再试


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    binance_ws_client.register_closed_kline_listener(
        market_agent_manager.on_closed_kline
    )
    db = next(get_db())
    try:
        s = db.query(Settings).first()
        provider = normalize_exchange_provider(
            getattr(s, "exchange_provider", None) if s else None
        )
        app_state.exchange_provider = provider
        key_enc, _ = active_keys(s) if s else (None, None)
        if provider == BINANCE_PROVIDER and key_enc:
            try:
                from .routes.settings import _connect_active

                await _connect_active(s)
                logger.info("Auto-reconnected to Binance on startup")
            except Exception as e:
                logger.warning(f"Auto-connect failed: {e}")
    finally:
        db.close()

    try:
        if app_state.exchange_provider == BINANCE_PROVIDER:
            await market_agent_manager.restore()
        else:
            await binance_ws_client.stop()
            app_state.disconnect_exchange(app_state.exchange_provider)
            await market_agent_manager.stop()
    except MarketAgentStateError:
        logger.error("Market agent startup rejected: a single Uvicorn worker is required")
        raise
    except Exception as exc:
        logger.warning(
            "Market agent restore failed: exception_type={}", type(exc).__name__
        )

    background_tasks = (
        asyncio.create_task(
            app_state.broadcast_loop(), name="app-state-broadcast-loop"
        ),
        asyncio.create_task(_reconnect_loop(), name="binance-reconnect-loop"),
    )
    app.state.background_tasks = background_tasks

    try:
        yield
    finally:
        binance_ws_client.register_closed_kline_listener(None)
        try:
            await market_agent_manager.shutdown()
        except Exception as exc:
            logger.warning(
                "Market agent shutdown failed: exception_type={}", type(exc).__name__
            )
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await binance_ws_client.stop()
        await strategy_route.bot_engine.stop()


app = FastAPI(title="CandleMind API", lifespan=lifespan)

_allowed_origins = [
    origin.strip()
    for origin in os.environ.get(
        "CANDLEMIND_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/ping")
def ping():
    return {"ok": True}


app.include_router(settings.router,        prefix="/api/settings",  tags=["settings"])
app.include_router(account.router,         prefix="/api/account",   tags=["account"])
app.include_router(market.router,          prefix="/api/market",    tags=["market"])
app.include_router(orders.router,          prefix="/api/orders",    tags=["orders"])
app.include_router(strategy_route.router,  prefix="/api/strategy",  tags=["strategy"])
app.include_router(strategy_analytics.router)
app.include_router(ai_config.router,       prefix="/api/ai",        tags=["ai"])
app.include_router(market_agent.router,     prefix="/api/ai",        tags=["ai"])
app.include_router(health.router,          prefix="/api/health",    tags=["health"])


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
