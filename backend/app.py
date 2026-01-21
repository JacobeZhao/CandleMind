from fastapi import FastAPI
from contextlib import asynccontextmanager

from backend.services.market_service import MarketDataService
from backend.services.auth_service import AuthService
from backend.api_routers.market_api import market_router, init_market
from backend.api_routers.auth_api import auth_router, init_auth
from backend.api_routers.ai_analysis_api import ai_analysis_router, init_ai_analysis  # 新增AI分析API
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
    init_ai_analysis(market)  # 初始化AI分析服务

    yield

    # 关闭服务（顺序与启动相反）
    await auth.stop()
    await market.stop()


app = FastAPI(
    title="CandleMind Service",
    lifespan=lifespan
)

# 挂载路由
app.include_router(market_router, prefix="/market")
app.include_router(auth_router, prefix="/auth")
app.include_router(ai_analysis_router, prefix="/ai")  # 新增AI分析API路由