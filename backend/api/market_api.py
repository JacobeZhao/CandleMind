from fastapi import APIRouter, HTTPException
from market.market_data_service import MarketDataService, SYMBOLS, INTERVALS

router = APIRouter()
market: MarketDataService | None = None


def init_market(service: MarketDataService):
    global market
    market = service


@router.get("/health")
def health():
    return {
        "status": "ok",
        "market_ready": market.ready if market else False,
    }


@router.get("/symbols")
def symbols():
    return SYMBOLS


@router.get("/intervals")
def intervals():
    return INTERVALS


@router.get("/klines")
def klines(symbol: str, interval: str, limit: int = 100):
    if not market or not market.ready:
        raise HTTPException(503, "market not ready")

    symbol = symbol.lower()
    return market.data[symbol][interval].snapshot()[-limit:]


@router.get("/klines/latest")
def latest(symbol: str, interval: str):
    if not market or not market.ready:
        raise HTTPException(503, "market not ready")

    return market.data[symbol][interval].current
