import asyncio

from fastapi import APIRouter, HTTPException, Query

from ..services.account_trade_analytics import AccountTradeAnalyticsService
from ..services.binance_errors import BinanceGatewayError
from ..services.binance_usdm_gateway import (
    BinanceUsdMGateway,
    exchange_scope,
    gateway_error_detail,
    gateway_error_status,
)
from ..services.open_order_service import OpenOrderService
from ..services.exchange_provider import is_binance_provider, unavailable_provider_detail
from ..state import app_state

router = APIRouter()
open_order_service = OpenOrderService()
account_analytics_service = AccountTradeAnalyticsService()


def _require_client():
    if not is_binance_provider(app_state.exchange_provider):
        raise HTTPException(
            status_code=503,
            detail=unavailable_provider_detail(app_state.exchange_provider),
        )
    if not app_state.client:
        raise HTTPException(status_code=503, detail={
            "code": "binance_connection_required",
            "message": "尚未连接 Binance，请先在设置页完成配置。",
            "retryable": False,
        })
    return app_state.client


def _current_symbol(symbol: str | None) -> str:
    active = app_state.symbol.strip().upper()
    requested = (symbol or active).strip().upper()
    if requested != active:
        raise HTTPException(status_code=409, detail={
            "code": "scope_conflict",
            "message": "请求品种与当前品种不一致，请刷新后重试。",
            "retryable": True,
        })
    return requested


def _safe_gateway_error(exc: BinanceGatewayError) -> HTTPException:
    return HTTPException(status_code=gateway_error_status(exc), detail=gateway_error_detail(exc))


@router.get("/open")
async def open_orders(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
):
    gateway = BinanceUsdMGateway(_require_client())
    requested = symbol or app_state.symbol
    try:
        return await asyncio.to_thread(gateway.open_orders, requested)
    except BinanceGatewayError as exc:
        raise _safe_gateway_error(exc) from exc


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
    except BinanceGatewayError as exc:
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
    except BinanceGatewayError as exc:
        raise _safe_gateway_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail={
            "code": "upstream_rejected",
            "message": "Binance returned an invalid response",
            "retryable": False,
        }) from exc


@router.get("/history")
async def order_history(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    gateway = BinanceUsdMGateway(_require_client())
    sym = symbol or app_state.symbol
    try:
        result = await asyncio.to_thread(gateway.all_orders, symbol=sym, limit=limit)
        return list(reversed(result))
    except BinanceGatewayError as exc:
        raise _safe_gateway_error(exc) from exc


@router.get("/trades")
async def recent_trades(
    symbol: str | None = Query(default=None, pattern=r"^[A-Z0-9]{5,20}$"),
    limit: int = Query(default=50, ge=1, le=1000),
):
    gateway = BinanceUsdMGateway(_require_client())
    sym = symbol or app_state.symbol
    try:
        return await asyncio.to_thread(gateway.account_trades, symbol=sym, limit=limit)
    except BinanceGatewayError as exc:
        raise _safe_gateway_error(exc) from exc
