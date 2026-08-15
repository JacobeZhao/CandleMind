from contextlib import asynccontextmanager
import asyncio
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .database import init_db, get_db, Settings, active_keys
from .security import decrypt
from .state import app_state
from .binance_ws import binance_ws_client
from .ws_manager import manager
from .routes import settings, account, market, orders, backtest, ai_config, health
from .routes import strategy as strategy_route
from .routes.settings import _build_client


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
                if s and active_keys(s)[0]:
                    await _connect_active(s)
                    logger.info("Auto-reconnect succeeded")
            except Exception:
                pass   # 仍连不上（如节点受限），下个周期再试


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = next(get_db())
    try:
        s = db.query(Settings).first()
        key_enc, sec_enc = active_keys(s) if s else (None, None)
        if key_enc:
            try:
                api_key = decrypt(key_enc)
                api_secret = decrypt(sec_enc)
                client = await asyncio.to_thread(
                    _build_client, api_key, api_secret, s.testnet, s.proxy_url
                )
                app_state.set_client(client, s.symbol)
                await binance_ws_client.start(s.symbol, s.testnet, s.proxy_url)
                logger.info("Auto-reconnected to Binance on startup")
            except Exception as e:
                logger.warning(f"Auto-connect failed: {e}")
    finally:
        db.close()

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
app.include_router(backtest.router,        prefix="/api/backtest",  tags=["backtest"])
app.include_router(ai_config.router,       prefix="/api/ai",        tags=["ai"])
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
