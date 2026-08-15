from fastapi import APIRouter, HTTPException, Query
import asyncio
from ..state import app_state

router = APIRouter()


def _require_client():
    if not app_state.client:
        raise HTTPException(status_code=503, detail="未连接 Binance，请先配置 API Key")
    return app_state.client


@router.get("/open")
async def open_orders(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
):
    client = _require_client()
    kwargs = {"symbol": symbol} if symbol else {}
    return await asyncio.to_thread(client.futures_get_open_orders, **kwargs)


@router.get("/history")
async def order_history(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    client = _require_client()
    sym = symbol or app_state.symbol
    orders = await asyncio.to_thread(client.futures_get_all_orders, symbol=sym, limit=limit)
    return list(reversed(orders))


@router.get("/trades")
async def recent_trades(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    client = _require_client()
    sym = symbol or app_state.symbol
    return await asyncio.to_thread(client.futures_account_trades, symbol=sym, limit=limit)
