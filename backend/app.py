from fastapi import FastAPI
from contextlib import asynccontextmanager

from CandleMind.backend.services.market_service import MarketDataService
from CandleMind.backend.services.auth_service import AuthService
from CandleMind.backend.api.market_api import market_router, init_market
from CandleMind.backend.api.auth_api import auth_router, init_auth
from env_loader import env_loader

# 加载环境变量
env_loader.load()

# 初始化服务
market = MarketDataService()
auth = AuthService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动服务
    await market.start()
    await auth.start()

    # 注入 API 依赖
    init_market(market)
    init_auth(auth)

    yield

    # 关闭服务（顺序与启动相反）
    await auth.stop()
    await market.stop()


app = FastAPI(
    title="CandleMind Market Service",
    lifespan=lifespan
)

# 挂载路由
app.include_router(market_router)
app.include_router(auth_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "market": market.ready if market else False,
        "auth": auth.ready if auth else False,
    }
