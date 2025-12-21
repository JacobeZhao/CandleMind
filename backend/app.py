from fastapi import FastAPI
from contextlib import asynccontextmanager

from market.market_data_service import MarketDataService
from api.market_api import router, init_market

market = MarketDataService()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动市场服务，拉取历史数据，启动 WS
    await market.start()
    # 初始化 API 相关依赖
    init_market(market)
    yield
    # 可选：关闭市场服务
    await market.stop()

app = FastAPI(
    title="CandleMind Market Service",
    lifespan=lifespan
)

# 挂载路由
app.include_router(router)
