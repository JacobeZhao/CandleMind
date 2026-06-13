from fastapi import APIRouter, HTTPException
import asyncio
from ..state import app_state

router = APIRouter()


def _require_client():
    if not app_state.client:
        raise HTTPException(status_code=503, detail="未连接 Binance，请先配置 API Key")
    return app_state.client


@router.get("/balance")
async def get_balance():
    client = _require_client()
    balances = await asyncio.to_thread(client.futures_account_balance)
    account  = await asyncio.to_thread(client.futures_account)
    return {
        "balances": [b for b in balances if float(b.get("balance", 0)) > 0],
        "totalWalletBalance": account.get("totalWalletBalance"),
        "totalUnrealizedProfit": account.get("totalUnrealizedProfit"),
        "totalMarginBalance": account.get("totalMarginBalance"),
        "availableBalance": next(
            (b["availableBalance"] for b in balances if b["asset"] == "USDT"), "0"
        ),
    }


@router.get("/positions")
async def get_positions():
    client = _require_client()
    positions = await asyncio.to_thread(client.futures_position_information)
    return [p for p in positions if float(p.get("positionAmt", 0)) != 0]
