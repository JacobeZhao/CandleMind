"""Lifecycle and event APIs for the read-only continuous market agent."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.market_agent import MarketAgentError, market_agent_manager


router = APIRouter()


class MarketAgentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=20)
    interval: str | None = Field(default=None, max_length=10)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != value.upper() or not re.fullmatch(r"[A-Z0-9]{2,16}USDT", value):
            raise ValueError("symbol must be an uppercase USDT futures symbol")
        return value



class MarketAgentMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=20)
    content: str = Field(min_length=1, max_length=1000)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != value.upper() or not re.fullmatch(r"[A-Z0-9]{2,16}USDT", value):
            raise ValueError("symbol must be an uppercase USDT futures symbol")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("message content cannot be blank")
        return content


def _raise_api_error(exc: MarketAgentError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    ) from exc


@router.post("/market-agent/start")
async def start_market_agent(body: MarketAgentStartRequest):
    try:
        return await market_agent_manager.start(symbol=body.symbol)
    except MarketAgentError as exc:
        _raise_api_error(exc)


@router.post("/market-agent/messages")
async def send_market_agent_message(body: MarketAgentMessageRequest):
    try:
        return await market_agent_manager.message(symbol=body.symbol, content=body.content)
    except MarketAgentError as exc:
        _raise_api_error(exc)


@router.post("/market-agent/stop")
async def stop_market_agent():
    return await market_agent_manager.stop()


@router.get("/market-agent/status")
def market_agent_status():
    return market_agent_manager.status()


@router.get("/market-agent/events")
def market_agent_events(
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    events = market_agent_manager.events(after_sequence=after_sequence, limit=limit)
    return {"events": events, "latest_sequence": market_agent_manager.status()["latest_sequence"]}
