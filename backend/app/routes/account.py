import asyncio

from fastapi import APIRouter, HTTPException

from ..services.binance_errors import BinanceGatewayError, BinanceGatewayRejected
from ..services.binance_usdm_gateway import (
    BinanceUsdMGateway,
    gateway_error_detail,
    gateway_error_status,
)
from ..services.exchange_provider import is_binance_provider, unavailable_provider_detail
from ..state import app_state

router = APIRouter()


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


def _gateway_http_error(exc: BinanceGatewayError) -> HTTPException:
    return HTTPException(status_code=gateway_error_status(exc), detail=gateway_error_detail(exc))


@router.get("/balance")
async def get_balance():
    gateway = BinanceUsdMGateway(_require_client())
    try:
        balances, account = await asyncio.gather(
            asyncio.to_thread(gateway.account_balance),
            asyncio.to_thread(gateway.account),
        )
        return {
            "balances": [b for b in balances if float(b.get("balance", 0)) > 0],
            "totalWalletBalance": account.get("totalWalletBalance"),
            "totalUnrealizedProfit": account.get("totalUnrealizedProfit"),
            "totalMarginBalance": account.get("totalMarginBalance"),
            "availableBalance": next(
                (b["availableBalance"] for b in balances if b["asset"] == "USDT"), "0"
            ),
        }
    except BinanceGatewayError as exc:
        raise _gateway_http_error(exc) from exc
    except (KeyError, TypeError, ValueError) as exc:
        error = BinanceGatewayRejected("Binance returned an invalid response")
        raise _gateway_http_error(error) from exc


@router.get("/positions")
async def get_positions():
    gateway = BinanceUsdMGateway(_require_client())
    try:
        positions = await asyncio.to_thread(gateway.position_information)
        return [p for p in positions if float(p.get("positionAmt", 0)) != 0]
    except BinanceGatewayError as exc:
        raise _gateway_http_error(exc) from exc
    except (TypeError, ValueError) as exc:
        error = BinanceGatewayRejected("Binance returned an invalid response")
        raise _gateway_http_error(error) from exc
