from typing import Literal

from binance.exceptions import BinanceAPIException, BinanceRequestException
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from ..services.bot_engine import bot_engine
from ..services.sar_adx_runtime import SarAdxRuntimeError
from ..services.sar_adx_state_store import SarAdxStateError
from ..state import app_state


router = APIRouter()


class EngineStartRequest(BaseModel):
    strategy_type: Literal["sar_adx_pyramid"] = "sar_adx_pyramid"
    config_version: Literal["sar_adx_v3"] = "sar_adx_v3"
    symbol: str = Field(min_length=5, max_length=20, pattern=r"^[A-Z0-9]+USDT$")
    paper: bool = True
    initial_capital: float = Field(default=10_000.0, gt=0.0, le=100_000_000.0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


@router.get("/engine/status")
def engine_status():
    bot_engine.hydrate_persisted_status(app_state.symbol)
    return bot_engine.status


@router.post("/engine/start")
async def start_engine(body: EngineStartRequest):
    if not body.paper:
        raise HTTPException(403, "SAR/ADX is restricted to paper trading")
    if not app_state.client:
        raise HTTPException(503, "Binance is not connected")
    cfg = {
        "name": "SAR + ADX Pyramid V3",
        "symbol": body.symbol,
        "interval": "5m",
        "check_interval": 15,
        "strategy_type": body.strategy_type,
        "paper": True,
        "config_version": body.config_version,
        "initial_capital": body.initial_capital,
    }
    try:
        await bot_engine.start(app_state.client, cfg)
    except (SarAdxRuntimeError, SarAdxStateError) as exc:
        raise HTTPException(409, f"Paper strategy state requires recovery: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (BinanceAPIException, BinanceRequestException) as exc:
        raise HTTPException(502, "Binance rejected or returned an invalid response") from exc
    except (
        TimeoutError,
        ConnectionError,
        RequestsTimeout,
        RequestsConnectionError,
    ) as exc:
        raise HTTPException(503, "Binance is temporarily unavailable") from exc
    status = bot_engine.status
    return {
        "ok": True,
        "message": f"SAR + ADX paper strategy started for {body.symbol}",
        "symbol": status.get("symbol", body.symbol),
        "strategy_type": status.get("strategy_type", body.strategy_type),
        "config_version": status.get("config_version", body.config_version),
    }


@router.post("/engine/stop")
async def stop_engine():
    await bot_engine.stop()
    return {"ok": True, "message": "SAR + ADX paper strategy stopped"}
