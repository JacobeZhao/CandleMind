import os

from fastapi import APIRouter, HTTPException
from backend.services.market_service import MarketDataService

market_router = APIRouter()
market: MarketDataService | None = None


def init_market(service: MarketDataService):
    """
    初始化全局市场数据服务实例
    
    Args:
        service (MarketDataService): 市场数据服务实例，将被设置为全局变量
    """
    global market
    market = service


@market_router.get("/symbols")
def symbols():
    return os.environ.get("SYMBOLS", "btcusdt,ethusdt,solusdt")


@market_router.get("/intervals")
def intervals():
    return os.environ.get("INTERVALS", "1m,5m,15m,30m,1h,2h,4h,6h,12h,1d,1w,1M")


@market_router.get("/klines")
def klines(symbol: str, interval: str, limit: int = 100):
    if not market or not market.ready:
        raise HTTPException(503, "market not ready")

    symbol = symbol.lower()
    return market.data[symbol][interval].snapshot()[-limit:]


@market_router.get("/klines/latest")
def latest(symbol: str, interval: str):
    if not market or not market.ready:
        raise HTTPException(503, "market not ready")

    return market.data[symbol][interval].current


@market_router.get("/")
def health():
    return {
        "status": "ok",
        "market": market.ready if market else False,
    }
