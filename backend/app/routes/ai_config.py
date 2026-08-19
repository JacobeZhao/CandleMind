from __future__ import annotations

import re
from threading import BoundedSemaphore
from time import monotonic
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from cryptography.fernet import InvalidToken
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ..database import AIConfig, Settings, get_db
from ..security import decrypt, encrypt
from ..state import app_state
from ..services.ai_config_validation import (
    AIConfigValidationError,
    ValidatedAIConfig,
    validate_ai_config,
)
from ..services.ai_provider import (
    AIProviderError,
    PROVIDER_DEFAULTS,
    PROVIDER_NAMES,
    test_connection,
)
from ..services.market_chat import MarketDataError, analyze_market

router = APIRouter()


class AIConfigIn(BaseModel):
    name: str
    provider: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class AITestDraftIn(AIConfigIn):
    config_id: Optional[int] = None


class MarketChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str = Field(min_length=1, max_length=1000)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("message content cannot be blank")
        return content


class MarketChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=20)
    interval: str
    messages: list[MarketChatMessage] = Field(min_length=1, max_length=10)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != value.upper() or not re.fullmatch(r"[A-Z0-9]{2,16}USDT", value):
            raise ValueError("symbol must be an uppercase USDT futures symbol")
        return value

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        if value not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("unsupported interval")
        return value

    @model_validator(mode="after")
    def validate_conversation(self):
        expected = "user"
        total_length = 0
        for message in self.messages:
            if message.role != expected:
                raise ValueError("messages must alternate user and assistant roles")
            expected = "assistant" if expected == "user" else "user"
            total_length += len(message.content)
        if self.messages[-1].role != "user":
            raise ValueError("conversation must end with a user message")
        if total_length > 6000:
            raise ValueError("conversation is too long")
        return self


_market_chat_slots = BoundedSemaphore(2)


def _error(code: str, message: str, *, retryable: bool = False, status: int = 400):
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": message, "retryable": retryable},
    )


def _serialize(config: AIConfig) -> dict:
    defaults = PROVIDER_DEFAULTS.get(config.provider, {})
    return {
        "id": config.id,
        "name": config.name,
        "provider": config.provider,
        "api_key_set": bool(config.api_key_enc),
        "base_url": config.base_url or defaults.get("base_url", ""),
        "model_name": config.model_name or defaults.get("model", ""),
        "is_active": config.is_active,
    }


def _decrypted_key(config: AIConfig | None) -> str:
    return decrypt(config.api_key_enc) if config and config.api_key_enc else ""


def _validate(body: AIConfigIn, existing: AIConfig | None = None) -> ValidatedAIConfig:
    try:
        existing_key = ""
        if existing and existing.provider.strip().lower() == body.provider.strip().lower():
            existing_key = _decrypted_key(existing)
        return validate_ai_config(
            name=body.name,
            provider=body.provider,
            api_key=body.api_key,
            base_url=body.base_url,
            model_name=body.model_name,
            existing_api_key=existing_key,
        )
    except AIConfigValidationError as exc:
        _error("invalid_config", str(exc))
    except InvalidToken:
        logger.error("Stored AI API key cannot be decrypted: config_id={}", getattr(existing, "id", None))
        _error("invalid_config", "已保存的 API Key 无法解密，请重新输入")


def _provider_error(exc: Exception):
    if isinstance(exc, AIProviderError):
        _error(exc.code, exc.message, retryable=exc.retryable)
    logger.warning("Unexpected AI operation failure: exception_type={}", type(exc).__name__)
    _error("provider_unavailable", "无法连接 AI 服务", retryable=True)


async def _run_connection_test(
    config: ValidatedAIConfig,
    proxy: str | None,
    *,
    config_id: int | None,
) -> str:
    started = monotonic()
    host = urlsplit(config.base_url or "").hostname or "anthropic"
    try:
        result = await test_connection(config.as_provider_config(), proxy)
    except Exception as exc:
        logger.warning(
            "AI connection test failed: config_id={} provider={} model={} host={} elapsed_ms={} exception_type={}",
            config_id,
            config.provider,
            config.model_name,
            host,
            round((monotonic() - started) * 1000),
            type(exc).__name__,
        )
        _provider_error(exc)
    logger.info(
        "AI connection test succeeded: config_id={} provider={} model={} host={} elapsed_ms={}",
        config_id,
        config.provider,
        config.model_name,
        host,
        round((monotonic() - started) * 1000),
    )
    return result


@router.get("/providers")
def list_providers():
    return [
        {
            "id": provider,
            "name": PROVIDER_NAMES.get(provider, provider),
            "base_url": defaults["base_url"] or "",
            "model": defaults["model"],
        }
        for provider, defaults in PROVIDER_DEFAULTS.items()
    ]


@router.get("/list")
def list_configs(db: Session = Depends(get_db)):
    return [_serialize(config) for config in db.query(AIConfig).all()]


@router.post("/create")
def create_config(body: AIConfigIn, db: Session = Depends(get_db)):
    validated = _validate(body)
    config = AIConfig(
        name=validated.name,
        provider=validated.provider,
        api_key_enc=encrypt(validated.api_key) if validated.api_key else None,
        base_url=validated.base_url,
        model_name=validated.model_name,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return {"ok": True, "id": config.id}


@router.post("/test-draft")
async def test_draft(body: AITestDraftIn, db: Session = Depends(get_db)):
    existing = None
    if body.config_id is not None:
        existing = db.query(AIConfig).filter(AIConfig.id == body.config_id).first()
        if not existing:
            _error("config_not_found", "AI 配置不存在", status=404)
    validated = _validate(body, existing)
    settings = db.query(Settings).first()
    result = await _run_connection_test(
        validated,
        settings.proxy_url if settings else None,
        config_id=body.config_id,
    )
    return {"ok": True, "response": result}


@router.put("/{cfg_id}")
def update_config(cfg_id: int, body: AIConfigIn, db: Session = Depends(get_db)):
    config = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not config:
        _error("config_not_found", "AI 配置不存在", status=404)
    provider_changed = config.provider.strip().lower() != body.provider.strip().lower()
    validated = _validate(body, config)
    config.name = validated.name
    config.provider = validated.provider
    config.base_url = validated.base_url
    config.model_name = validated.model_name
    if (body.api_key or "").strip() not in {"", "_keep_"}:
        config.api_key_enc = encrypt(validated.api_key)
    elif provider_changed:
        config.api_key_enc = None
    db.commit()
    return {"ok": True}


@router.delete("/{cfg_id}")
def delete_config(cfg_id: int, db: Session = Depends(get_db)):
    config = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not config:
        _error("config_not_found", "AI 配置不存在", status=404)
    db.delete(config)
    db.commit()
    return {"ok": True}


@router.post("/{cfg_id}/activate")
def activate_config(cfg_id: int, db: Session = Depends(get_db)):
    config = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not config:
        _error("config_not_found", "AI 配置不存在", status=404)
    db.query(AIConfig).filter(AIConfig.id != cfg_id).update({AIConfig.is_active: False})
    config.is_active = True
    db.commit()
    return {"ok": True}


@router.post("/{cfg_id}/test")
async def test_config(cfg_id: int, db: Session = Depends(get_db)):
    config = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not config:
        _error("config_not_found", "AI 配置不存在", status=404)
    validated = _validate(
        AIConfigIn(
            name=config.name,
            provider=config.provider,
            base_url=config.base_url,
            model_name=config.model_name,
        ),
        config,
    )
    settings = db.query(Settings).first()
    result = await _run_connection_test(
        validated,
        settings.proxy_url if settings else None,
        config_id=cfg_id,
    )
    return {"ok": True, "response": result}


@router.post("/market-chat")
async def market_chat_endpoint(body: MarketChatRequest, db: Session = Depends(get_db)):
    if not _market_chat_slots.acquire(blocking=False):
        _error("market_chat_busy", "行情 AI 分析请求较多，请稍后重试", retryable=True, status=429)
    try:
        config = db.query(AIConfig).filter(AIConfig.is_active.is_(True)).first()
        if not config:
            _error("config_not_found", "请先激活一个 AI 配置")
        if not app_state.client:
            _error("market_unavailable", "Binance 行情服务不可用", retryable=True, status=503)
        validated = _validate(
            AIConfigIn(
                name=config.name,
                provider=config.provider,
                base_url=config.base_url,
                model_name=config.model_name,
            ),
            config,
        )
        settings = db.query(Settings).first()
        result = await analyze_market(
            client=app_state.client,
            symbol=body.symbol,
            interval=body.interval,
            messages=[message.model_dump() for message in body.messages],
            provider_config=validated.as_provider_config(),
            proxy_url=settings.proxy_url if settings else None,
        )
        return {
            "answer": result.answer,
            "context": {
                "symbol": body.symbol,
                "interval": body.interval,
                "snapshot_at": result.snapshot_at,
                "current_bar_closed_at": result.current_bar_closed_at,
                "adx_bar_closed_at": result.adx_bar_closed_at,
                "model_name": validated.model_name,
            },
        }
    except HTTPException:
        raise
    except MarketDataError as exc:
        logger.warning("Market chat data failure: exception_type={}", type(exc).__name__)
        _error("market_unavailable", "Binance 行情服务不可用", retryable=True, status=503)
    except Exception as exc:
        _provider_error(exc)
    finally:
        _market_chat_slots.release()
