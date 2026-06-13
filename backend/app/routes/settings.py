from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from binance.client import Client
from ..database import get_db, Settings, active_keys
from ..security import encrypt, decrypt
from ..state import app_state
from ..binance_ws import binance_ws_client
import asyncio
import requests as _requests

router = APIRouter()

CONNECT_TIMEOUT = 30  # 秒

_KEEP = ("_keep_", "")


class SettingsIn(BaseModel):
    # 两套 API（任一为空/"_keep_" 表示不改动）
    api_key_test:    Optional[str] = None
    api_secret_test: Optional[str] = None
    api_key_main:    Optional[str] = None
    api_secret_main: Optional[str] = None
    testnet: bool = True
    symbol:  str = "BTCUSDT"
    interval: str = "15m"
    proxy_url: Optional[str] = None


class SettingsOut(BaseModel):
    test_key_set: bool
    main_key_set: bool
    testnet:  bool
    symbol:   str
    interval: str
    proxy_url: Optional[str]
    connected: bool


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    return SettingsOut(
        test_key_set=bool(s.api_key_test_enc or (s.testnet and s.api_key_enc)),
        main_key_set=bool(s.api_key_main_enc),
        testnet=s.testnet,
        symbol=s.symbol,
        interval=s.interval,
        proxy_url=s.proxy_url or "",
        connected=app_state.client is not None,
    )


async def _connect_active(s) -> None:
    """用当前激活(testnet/main)的 key 建连并启动行情流。"""
    key_enc, sec_enc = active_keys(s)
    if not key_enc:
        raise HTTPException(400, f"当前为{'测试网' if s.testnet else '真实网'}模式，但未配置对应 API Key")
    client = await asyncio.to_thread(
        _build_client, decrypt(key_enc), decrypt(sec_enc), s.testnet, s.proxy_url
    )
    app_state.set_client(client, s.symbol)
    await binance_ws_client.start(s.symbol, s.testnet, s.proxy_url)


@router.post("")
async def save_settings(body: SettingsIn, db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    if body.api_key_test and body.api_key_test not in _KEEP:
        s.api_key_test_enc = encrypt(body.api_key_test.strip())
    if body.api_secret_test and body.api_secret_test not in _KEEP:
        s.api_secret_test_enc = encrypt(body.api_secret_test.strip())
    if body.api_key_main and body.api_key_main not in _KEEP:
        s.api_key_main_enc = encrypt(body.api_key_main.strip())
    if body.api_secret_main and body.api_secret_main not in _KEEP:
        s.api_secret_main_enc = encrypt(body.api_secret_main.strip())
    s.testnet = body.testnet
    s.symbol = body.symbol
    s.interval = body.interval
    s.proxy_url = body.proxy_url or None
    db.commit()

    key_enc, _ = active_keys(s)
    if not key_enc:
        return {"ok": True, "message": "配置已保存（当前模式未配置 API Key，暂未连接）"}
    try:
        await _connect_active(s)
        return {"ok": True, "message": f"保存成功，已连接 Binance（{'测试网' if s.testnet else '真实网'}）！"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"API 连接失败: {e}")


@router.post("/connect")
async def connect(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    try:
        await _connect_active(s)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/test-connection")
async def test_connection(testnet: bool = True, db: Session = Depends(get_db)):
    """独立测试测试网或真实网的 API Key 是否可用（不切换当前活跃连接）。"""
    s = db.query(Settings).first()
    if testnet:
        key_enc = s.api_key_test_enc or (s.api_key_enc if s.testnet else None)
        sec_enc = s.api_secret_test_enc or (s.api_secret_enc if s.testnet else None)
        net_name = "测试网"
    else:
        key_enc = s.api_key_main_enc or (s.api_key_enc if not s.testnet else None)
        sec_enc = s.api_secret_main_enc or (s.api_secret_enc if not s.testnet else None)
        net_name = "真实网"

    if not key_enc:
        raise HTTPException(400, f"{net_name} API Key 未配置")
    try:
        await asyncio.to_thread(
            _build_client, decrypt(key_enc), decrypt(sec_enc), testnet, s.proxy_url
        )
        return {"ok": True, "message": f"{net_name} 连接成功"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{net_name} 连接失败: {e}")


@router.get("/myip")
async def get_my_ip(db: Session = Depends(get_db)):
    """通过配置的代理检测出口 IP，用于 Binance API 白名单"""
    s = db.query(Settings).first()
    proxy_url = s.proxy_url if s else None

    proxies = None
    if proxy_url and proxy_url.strip():
        p = _rewrite_proxy(proxy_url.strip())
        proxies = {"http": p, "https": p}

    try:
        resp = await asyncio.to_thread(
            lambda: _requests.get(
                "https://api.ipify.org?format=json",
                proxies=proxies,
                timeout=15,
            )
        )
        ip = resp.json().get("ip", "未知")
        return {"ip": ip, "via_proxy": proxies is not None}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IP 检测失败: {e}")


def _rewrite_proxy(url: str) -> str:
    """Docker 容器内 localhost/127.0.0.1 不可达，替换为 host.docker.internal"""
    import os
    in_docker = os.path.exists("/.dockerenv") or os.getenv("DOCKER_CONTAINER")
    if not in_docker:
        return url
    return (url.replace("://localhost:", "://host.docker.internal:")
               .replace("://127.0.0.1:", "://host.docker.internal:"))


def _build_client(api_key: str, api_secret: str, testnet: bool,
                  proxy_url: str | None = None) -> Client:
    import time as _time
    requests_params: dict = {"timeout": CONNECT_TIMEOUT}
    if proxy_url and proxy_url.strip():
        p = _rewrite_proxy(proxy_url.strip())
        requests_params["proxies"] = {"http": p, "https": p}

    client = Client(api_key.strip(), api_secret.strip(),
                    requests_params=requests_params)

    if testnet:
        client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

    # 自动同步时间偏移，解决 -1021 timestamp 错误
    try:
        server_ts = client.futures_time()["serverTime"]
        client.timestamp_offset = server_ts - int(_time.time() * 1000)
    except Exception:
        pass

    client.futures_ping()
    return client
