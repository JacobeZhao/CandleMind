from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from ..database import get_db, AIConfig, Settings
from ..services.ai_provider import PROVIDER_DEFAULTS, PROVIDER_NAMES
from ..services.strategy_parser import parse_strategy, test_connection
from ..security import encrypt, decrypt

router = APIRouter()


class AIConfigIn(BaseModel):
    name:       str
    provider:   str
    api_key:    Optional[str] = None
    base_url:   Optional[str] = None
    model_name: Optional[str] = None


class ParseRequest(BaseModel):
    text: str


def _serialize(c: AIConfig) -> dict:
    defaults = PROVIDER_DEFAULTS.get(c.provider, {})
    return {
        "id":         c.id,
        "name":       c.name,
        "provider":   c.provider,
        "api_key_set": bool(c.api_key_enc),
        "base_url":   c.base_url or defaults.get("base_url", ""),
        "model_name": c.model_name or defaults.get("model", ""),
        "is_active":  c.is_active,
    }


@router.get("/providers")
def list_providers():
    return [
        {
            "id":       pid,
            "name":     PROVIDER_NAMES.get(pid, pid),
            "base_url": d["base_url"] or "",
            "model":    d["model"],
        }
        for pid, d in PROVIDER_DEFAULTS.items()
    ]


@router.get("/list")
def list_configs(db: Session = Depends(get_db)):
    return [_serialize(c) for c in db.query(AIConfig).all()]


@router.post("/create")
def create_config(body: AIConfigIn, db: Session = Depends(get_db)):
    c = AIConfig(
        name       = body.name,
        provider   = body.provider,
        base_url   = body.base_url or None,
        model_name = body.model_name or None,
    )
    if body.api_key and body.api_key not in ("_keep_", ""):
        c.api_key_enc = encrypt(body.api_key)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"ok": True, "id": c.id}


@router.put("/{cfg_id}")
def update_config(cfg_id: int, body: AIConfigIn, db: Session = Depends(get_db)):
    c = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    c.name       = body.name
    c.provider   = body.provider
    c.base_url   = body.base_url or None
    c.model_name = body.model_name or None
    if body.api_key and body.api_key not in ("_keep_", ""):
        c.api_key_enc = encrypt(body.api_key)
    db.commit()
    return {"ok": True}


@router.delete("/{cfg_id}")
def delete_config(cfg_id: int, db: Session = Depends(get_db)):
    c = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/{cfg_id}/activate")
def activate_config(cfg_id: int, db: Session = Depends(get_db)):
    db.query(AIConfig).update({AIConfig.is_active: False})
    c = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    c.is_active = True
    db.commit()
    return {"ok": True}


@router.post("/{cfg_id}/test")
async def test_config(cfg_id: int, db: Session = Depends(get_db)):
    c = db.query(AIConfig).filter(AIConfig.id == cfg_id).first()
    if not c:
        raise HTTPException(404, "配置不存在")
    s = db.query(Settings).first()
    proxy = s.proxy_url if s else None

    api_key = decrypt(c.api_key_enc) if c.api_key_enc else ""
    ai_cfg  = {
        "provider":   c.provider,
        "api_key":    api_key,
        "base_url":   c.base_url,
        "model_name": c.model_name,
    }
    try:
        result = await test_connection(ai_cfg, proxy)
        return {"ok": True, "response": result}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/parse")
async def parse_strategy_endpoint(body: ParseRequest, db: Session = Depends(get_db)):
    c = db.query(AIConfig).filter(AIConfig.is_active == True).first()
    if not c:
        raise HTTPException(400, "请先在设置中配置并激活一个 AI 模型")
    s = db.query(Settings).first()
    proxy = s.proxy_url if s else None

    api_key = decrypt(c.api_key_enc) if c.api_key_enc else ""
    ai_cfg  = {
        "provider":   c.provider,
        "api_key":    api_key,
        "base_url":   c.base_url,
        "model_name": c.model_name,
    }
    try:
        result = await parse_strategy(body.text, ai_cfg, proxy)
        return result
    except Exception as e:
        raise HTTPException(400, str(e))
