from typing import Any
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..services.bot_engine import bot_engine
from ..database import Settings, get_db
from ..services.exchange_executor import ExchangeExecutionError
from ..services.binance_errors import BinanceGatewayError
from ..services.binance_usdm_gateway import gateway_error_detail, gateway_error_status
from ..services.execution_store import ExecutionStoreError
from ..services.exchange_provider import (
    BINANCE_PROVIDER,
    is_binance_provider,
    normalize_exchange_provider,
    unavailable_provider_detail,
)
from ..services.live_strategy_runtime import LiveStrategyRuntimeError
from ..services.strategy_configuration import (
    StrategyConfigurationConflict,
    StrategyType,
    get_strategy_configuration,
    save_strategy_configuration,
    strategy_catalog,
)
from ..services.strategy_runtime_intent import (
    StrategyRuntimeIntentError,
    StrategyRuntimeIntentStore,
    StrategyRuntimeLeaseConflict,
    StrategyScope,
)
from ..state import app_state
from sqlalchemy.orm import Session


router = APIRouter()
runtime_intent_store = StrategyRuntimeIntentStore()
recovery_stop_lease = None


def audit_runtime_intent_on_startup() -> dict[str, Any]:
    audit = runtime_intent_store.audit_restart()
    bot_engine.apply_restart_audit(audit)
    return audit


def _require_binance_provider(settings) -> str:
    provider = normalize_exchange_provider(
        getattr(settings, "exchange_provider", None)
    )
    if not is_binance_provider(provider):
        raise HTTPException(503, detail=unavailable_provider_detail(provider))
    if app_state.exchange_provider != BINANCE_PROVIDER:
        raise HTTPException(
            503, detail=unavailable_provider_detail(app_state.exchange_provider)
        )
    return provider


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
    _require_binance_provider(settings)
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
    provider = normalize_exchange_provider(
        getattr(settings, "exchange_provider", None)
    )
    if (
        not is_binance_provider(provider)
        or app_state.exchange_provider != BINANCE_PROVIDER
    ):
        return {**bot_engine.status, "provider": provider}
    network = "testnet" if settings.testnet else "mainnet"
    bot_engine.hydrate_persisted_status(app_state.symbol, network)
    return {**bot_engine.status, "provider": provider}


@router.post("/engine/start")
async def start_engine(body: EngineStartRequest, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    provider = _require_binance_provider(settings)
    if not app_state.client:
        raise HTTPException(503, "Binance is not connected")
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
    scope = StrategyScope(provider, network, body.symbol)
    lease = None
    try:
        runtime_intent_store.request_start(scope, cfg)
        if not bot_engine.running:
            lease = runtime_intent_store.acquire_lease(
                scope,
                runtime_id=f"engine-{os.getpid()}-{body.symbol.lower()}",
                ttl_seconds=60,
            )
            cfg.update(
                {
                    "_intent_store": runtime_intent_store,
                    "_trading_lease": lease,
                    "_runtime_scope": scope,
                }
            )
        await bot_engine.start(app_state.client, cfg)
    except (
        LiveStrategyRuntimeError,
        ExchangeExecutionError,
        ExecutionStoreError,
        StrategyRuntimeIntentError,
    ) as exc:
        if lease is not None and not bot_engine.running:
            try:
                runtime_intent_store.release_lease(lease)
            except StrategyRuntimeLeaseConflict:
                pass
        raise HTTPException(409, f"Strategy execution requires recovery: {exc}") from exc
    except ValueError as exc:
        if lease is not None and not bot_engine.running:
            try:
                runtime_intent_store.release_lease(lease)
            except StrategyRuntimeLeaseConflict:
                pass
        raise HTTPException(409, str(exc)) from exc
    except BinanceGatewayError as exc:
        if lease is not None and not bot_engine.running:
            try:
                runtime_intent_store.release_lease(lease)
            except StrategyRuntimeLeaseConflict:
                pass
        raise HTTPException(
            gateway_error_status(exc), detail=gateway_error_detail(exc)
        ) from exc
    status = bot_engine.status
    return {
        "ok": True,
        "message": f"CandleMind strategy started for {body.symbol}",
        "symbol": status.get("symbol", body.symbol),
        "network": status.get("network", network),
        "strategy_type": status.get("strategy_type", body.strategy_type),
        "config_version": status.get("config_version", body.config_version),
        "config_hash": status.get("config_hash", body.config_hash),
        "provider": provider,
    }


@router.post("/engine/stop")
async def stop_engine(db: Session = Depends(get_db)):
    global recovery_stop_lease
    intent = runtime_intent_store.load()
    persisted_lease = None
    persisted_stopped = False
    try:
        if (
            not bot_engine.running
            and isinstance(intent, dict)
            and intent.get("desired_state") == "running"
        ):
            settings = db.query(Settings).first()
            provider = _require_binance_provider(settings)
            scope = StrategyScope(**intent["scope"])
            network = "testnet" if settings.testnet else "mainnet"
            if scope != StrategyScope(provider, network, settings.symbol):
                raise StrategyRuntimeIntentError(
                    "persisted strategy scope does not match the active connection"
                )
            if app_state.client is None:
                raise StrategyRuntimeIntentError(
                    "connect Binance before stopping the persisted strategy"
                )
            if recovery_stop_lease is not None:
                if recovery_stop_lease.scope != scope:
                    raise StrategyRuntimeLeaseConflict(
                        "another persisted scope is already under recovery"
                    )
                persisted_lease = runtime_intent_store.renew_lease(
                    recovery_stop_lease, ttl_seconds=60
                )
                recovery_stop_lease = persisted_lease
            else:
                lease_audit = runtime_intent_store.audit_lease(scope)
                if lease_audit["status"] == "stale_confirmed":
                    runtime_intent_store.reclaim_stale_lease(
                        scope,
                        expected_lease_id=lease_audit["lease"].lease_id,
                        reason="operator requested recovery stop after process restart",
                    )
                elif lease_audit["status"] != "absent":
                    raise StrategyRuntimeLeaseConflict(
                        f"persisted runtime has a {lease_audit['status']} lease"
                    )
                persisted_lease = runtime_intent_store.acquire_lease(
                    scope,
                    runtime_id=f"recovery-stop-{os.getpid()}-{scope.symbol.lower()}",
                    ttl_seconds=60,
                )
                recovery_stop_lease = persisted_lease
            await bot_engine.stop_persisted(app_state.client, network, scope.symbol)
            runtime_intent_store.request_stop(scope, intent["config"])
            persisted_stopped = True
        else:
            await bot_engine.stop()
    except (
        ExchangeExecutionError,
        ExecutionStoreError,
        StrategyRuntimeIntentError,
        ValueError,
    ) as exc:
        raise HTTPException(409, f"Strategy stop requires reconciliation: {exc}") from exc
    finally:
        if persisted_stopped and persisted_lease is not None:
            try:
                runtime_intent_store.release_lease(persisted_lease)
                recovery_stop_lease = None
            except StrategyRuntimeLeaseConflict:
                pass
    return {"ok": True, "message": "CandleMind trend strategy stopped"}
