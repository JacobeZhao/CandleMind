from typing import Any
import os

from binance.exceptions import BinanceAPIException, BinanceRequestException
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from ..services.bot_engine import bot_engine
from ..database import Settings, get_db
from ..services.exchange_executor import ExchangeExecutionError
from ..services.execution_store import ExecutionStoreError
from ..services.live_strategy_runtime import LiveStrategyRuntimeError
from ..services.strategy_configuration import (
    StrategyConfigurationConflict,
    StrategyType,
    get_strategy_configuration,
    save_strategy_configuration,
    strategy_catalog,
)
from ..state import app_state
from sqlalchemy.orm import Session


router = APIRouter()


class EngineStartRequest(BaseModel):
    strategy_type: StrategyType
    config_version: str = Field(min_length=1, max_length=40)
    config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    symbol: str = Field(min_length=5, max_length=20, pattern=r"^[A-Z0-9]+USDT$")
    capital_limit: float = Field(default=1_000.0, gt=0.0, le=100_000_000.0)
    mainnet_confirmation: str | None = Field(default=None, max_length=64)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class StrategyConfigurationUpdate(BaseModel):
    strategy_type: StrategyType
    parameters: dict[str, Any]
    expected_config_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )


@router.get("/catalog")
def get_strategy_catalog():
    return {"strategies": strategy_catalog()}


@router.get("/config")
def get_saved_strategy_configuration(db: Session = Depends(get_db)):
    try:
        return get_strategy_configuration(db)
    except StrategyConfigurationConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/config")
async def update_strategy_configuration(
    body: StrategyConfigurationUpdate, db: Session = Depends(get_db)
):
    if bot_engine.running or getattr(bot_engine, "engine_state", "stopped") != "stopped":
        raise HTTPException(409, "Stop the running strategy before changing its configuration")
    settings = db.query(Settings).first()
    network = "testnet" if settings.testnet else "mainnet"
    if app_state.client is None:
        if bot_engine.has_execution_journal(settings.symbol, network):
            raise HTTPException(
                409,
                "Connect Binance to verify the existing execution journal before changing configuration",
            )
    else:
        try:
            await bot_engine.assert_configuration_change_safe(
                app_state.client, settings.symbol, network
            )
        except (ValueError, ExchangeExecutionError, ExecutionStoreError) as exc:
            raise HTTPException(409, str(exc)) from exc
    try:
        return save_strategy_configuration(
            db,
            strategy_type=body.strategy_type,
            parameters=body.parameters,
            expected_config_hash=body.expected_config_hash,
        )
    except StrategyConfigurationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/engine/status")
def engine_status(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    network = "testnet" if settings.testnet else "mainnet"
    bot_engine.hydrate_persisted_status(app_state.symbol, network)
    return bot_engine.status


@router.post("/engine/start")
async def start_engine(body: EngineStartRequest, db: Session = Depends(get_db)):
    if not app_state.client:
        raise HTTPException(503, "Binance is not connected")
    settings = db.query(Settings).first()
    network = "testnet" if settings.testnet else "mainnet"
    try:
        saved = get_strategy_configuration(db)
    except StrategyConfigurationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if (
        body.strategy_type != saved["strategy_type"]
        or body.config_version != saved["config_version"]
        or body.config_hash != saved["config_hash"]
    ):
        raise HTTPException(409, "Strategy configuration changed; reload before starting")
    if body.symbol != settings.symbol or body.symbol != app_state.symbol:
        raise HTTPException(409, "Selected symbol is not bound to the active Binance connection")
    if network == "mainnet":
        enabled = os.environ.get("CANDLEMIND_MAINNET_TRADING_ENABLED", "").lower() in {
            "1", "true", "yes",
        }
        if not enabled:
            raise HTTPException(403, "Mainnet strategy execution is disabled by the server")
        if body.mainnet_confirmation != f"MAINNET:{body.symbol}":
            raise HTTPException(403, "Mainnet confirmation text is invalid")
    names = {item["strategy_type"]: item["name"] for item in strategy_catalog()}
    cfg = {
        "name": names[saved["strategy_type"]],
        "symbol": body.symbol,
        "interval": "5m",
        "check_interval": 15,
        "strategy_type": saved["strategy_type"],
        "config_version": saved["config_version"],
        "config_hash": saved["config_hash"],
        "parameters": saved["parameters"],
        "capital_limit": body.capital_limit,
        "network": network,
    }
    try:
        await bot_engine.start(app_state.client, cfg)
    except (LiveStrategyRuntimeError, ExchangeExecutionError, ExecutionStoreError) as exc:
        raise HTTPException(409, f"Strategy execution requires recovery: {exc}") from exc
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
        "message": f"CandleMind strategy started for {body.symbol}",
        "symbol": status.get("symbol", body.symbol),
        "network": status.get("network", network),
        "strategy_type": status.get("strategy_type", body.strategy_type),
        "config_version": status.get("config_version", body.config_version),
        "config_hash": status.get("config_hash", body.config_hash),
    }


@router.post("/engine/stop")
async def stop_engine():
    try:
        await bot_engine.stop()
    except (ExchangeExecutionError, ExecutionStoreError) as exc:
        raise HTTPException(409, f"Strategy stop requires reconciliation: {exc}") from exc
    return {"ok": True, "message": "CandleMind trend strategy stopped"}
