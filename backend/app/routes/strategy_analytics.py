"""Backend-authoritative strategy analytics endpoints."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import Settings, active_keys, get_db
from ..security import decrypt
from ..services.strategy_analytics import StrategyAnalyticsService, account_fingerprint
from ..services.strategy_analytics_store import StrategyAnalyticsStoreError
from ..state import app_state


router = APIRouter(prefix="/api/strategy/analytics", tags=["strategy-analytics"])
analytics_service = StrategyAnalyticsService()


def _active_scope(db: Session) -> tuple[object, str, str, str]:
    settings = db.query(Settings).first()
    if settings is None or app_state.client is None:
        raise HTTPException(503, "Binance is not connected")
    if settings.symbol != app_state.symbol:
        raise HTTPException(409, "Active symbol binding is inconsistent")
    encrypted_key, _ = active_keys(settings)
    if not encrypted_key:
        raise HTTPException(503, "Active account credential is unavailable")
    try:
        active_key = decrypt(encrypted_key)
    except Exception as exc:
        raise HTTPException(503, "Active account identity is unavailable") from exc
    client_key = getattr(app_state.client, "API_KEY", None)
    if not isinstance(client_key, str) or not hmac.compare_digest(active_key, client_key):
        raise HTTPException(409, "Active account binding is changing; retry shortly")
    fingerprint = account_fingerprint(active_key)
    network = "testnet" if settings.testnet else "mainnet"
    return app_state.client, fingerprint, network, settings.symbol


@router.get("")
async def get_strategy_analytics(db: Session = Depends(get_db)):
    client, fingerprint, network, symbol = _active_scope(db)
    scope_id = analytics_service.store.ensure_scope(fingerprint, network, symbol)
    refresh: dict = {"status": "not_started"}
    try:
        refresh = await asyncio.to_thread(
            analytics_service.sync, client, fingerprint, network, symbol
        )
    except Exception:
        refresh = {"status": "failed", "reasons": ["refresh_failed"]}
    try:
        snapshot = analytics_service.snapshot(scope_id)
    except (StrategyAnalyticsStoreError, ValueError) as exc:
        raise HTTPException(503, "Strategy analytics are unavailable") from exc
    snapshot["refresh"] = refresh
    snapshot["stale"] = refresh.get("status") not in {"complete", "cooldown"}
    return snapshot


@router.post("/sync")
async def sync_strategy_analytics(db: Session = Depends(get_db)):
    client, fingerprint, network, symbol = _active_scope(db)
    try:
        result = await asyncio.to_thread(
            analytics_service.sync, client, fingerprint, network, symbol, force=True
        )
        result["snapshot"] = analytics_service.snapshot(result["scope_id"])
        return result
    except (StrategyAnalyticsStoreError, ValueError) as exc:
        raise HTTPException(503, "Strategy analytics synchronization failed") from exc
    except Exception as exc:
        raise HTTPException(502, "Exchange analytics synchronization failed") from exc
