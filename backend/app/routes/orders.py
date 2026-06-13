from fastapi import APIRouter, HTTPException, Query
import asyncio
from ..state import app_state

router = APIRouter()


def _require_client():
    if not app_state.client:
        raise HTTPException(status_code=503, detail="未连接 Binance，请先配置 API Key")
    return app_state.client


@router.get("/open")
async def open_orders(symbol: str = Query(None)):
    client = _require_client()
    kwargs = {"symbol": symbol} if symbol else {}
    return await asyncio.to_thread(client.futures_get_open_orders, **kwargs)


@router.get("/history")
async def order_history(symbol: str = Query(None), limit: int = Query(50)):
    client = _require_client()
    sym = symbol or app_state.symbol
    orders = await asyncio.to_thread(client.futures_get_all_orders, symbol=sym, limit=limit)
    return list(reversed(orders))


@router.get("/trades")
async def recent_trades(symbol: str = Query(None), limit: int = Query(50)):
    client = _require_client()
    sym = symbol or app_state.symbol
    return await asyncio.to_thread(client.futures_account_trades, symbol=sym, limit=limit)


@router.delete("/cancel/{symbol}/{order_id}")
async def cancel_order(symbol: str, order_id: int):
    client = _require_client()
    return await asyncio.to_thread(client.futures_cancel_order, symbol=symbol, orderId=order_id)
