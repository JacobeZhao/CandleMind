import asyncio

from fastapi import APIRouter, HTTPException, Query

from ..services.account_trade_analytics import AccountTradeAnalyticsService
from ..services.binance_usdm_gateway import (
    BinanceGatewayAuthenticationError,
    BinanceGatewayRejected,
    BinanceGatewayUnavailable,
    BinanceUsdMGateway,
    exchange_scope,
)
from ..services.open_order_service import OpenOrderService
from ..state import app_state

router = APIRouter()
open_order_service = OpenOrderService()
account_analytics_service = AccountTradeAnalyticsService()


def _require_client():
    if not app_state.client:
        raise HTTPException(status_code=503, detail="未连接 Binance，请先配置 API Key")
    return app_state.client


def _current_symbol(symbol: str | None) -> str:
    active = app_state.symbol.strip().upper()
    requested = (symbol or active).strip().upper()
    if requested != active:
        raise HTTPException(status_code=409, detail="Requested symbol is not the active symbol")
    return requested


def _safe_gateway_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BinanceGatewayAuthenticationError):
        return HTTPException(
            status_code=401,
            detail="Binance 拒绝了 API Key、合约权限或当前出口 IP，请检查白名单",
        )
    if isinstance(exc, BinanceGatewayUnavailable):
        return HTTPException(status_code=503, detail="Binance is temporarily unavailable")
    return HTTPException(status_code=502, detail="Binance rejected or returned an invalid response")


@router.get("/open")
async def open_orders(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
):
    client = _require_client()
    kwargs = {"symbol": symbol} if symbol else {}
    return await asyncio.to_thread(client.futures_get_open_orders, **kwargs)


@router.get("/open/combined")
async def combined_open_orders(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
):
    client = _require_client()
    requested = _current_symbol(symbol)
    try:
        scope = exchange_scope(client, requested)
        return await asyncio.to_thread(
            open_order_service.combined, BinanceUsdMGateway(client), scope
        )
    except (BinanceGatewayUnavailable, BinanceGatewayRejected) as exc:
        raise _safe_gateway_error(exc) from exc


@router.get("/analytics")
async def account_trade_analytics(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
):
    client = _require_client()
    requested = _current_symbol(symbol)
    try:
        scope = exchange_scope(client, requested)
        return await asyncio.to_thread(
            account_analytics_service.snapshot, BinanceUsdMGateway(client), scope
        )
    except (BinanceGatewayUnavailable, BinanceGatewayRejected) as exc:
        raise _safe_gateway_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Binance rejected or returned an invalid response",
        ) from exc


@router.get("/history")
async def order_history(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    client = _require_client()
    sym = symbol or app_state.symbol
    try:
        orders = await asyncio.to_thread(
            BinanceUsdMGateway(client).all_orders, symbol=sym, limit=limit
        )
        return list(reversed(orders))
    except (BinanceGatewayUnavailable, BinanceGatewayRejected) as exc:
        raise _safe_gateway_error(exc) from exc


@router.get("/trades")
async def recent_trades(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    client = _require_client()
    sym = symbol or app_state.symbol
    try:
        return await asyncio.to_thread(
            BinanceUsdMGateway(client).account_trades, symbol=sym, limit=limit
        )
    except (BinanceGatewayUnavailable, BinanceGatewayRejected) as exc:
        raise _safe_gateway_error(exc) from exc
