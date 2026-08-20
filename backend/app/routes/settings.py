from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional
from sqlalchemy.orm import Session
from binance.client import Client
from ..database import get_db, Settings, active_keys
from ..security import encrypt, decrypt
from ..state import app_state
from ..binance_ws import binance_ws_client
from ..proxy import rewrite_proxy_for_runtime
import asyncio
import re
import requests as _requests

router = APIRouter()

CONNECT_TIMEOUT = 30  # 秒
WS_READY_TIMEOUT = 10  # Must fail before the frontend's 15s settings timeout.

_KEEP = ("_keep_", "")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)?$")
_settings_lock = asyncio.Lock()
_ws_switch_lock = asyncio.Lock()


class _FuturesClient(Client):
    """Avoid python-binance's unrelated spot ping during construction."""

    def ping(self):
        return {}


class SettingsIn(BaseModel):
    # 两套 API（任一为空/"_keep_" 表示不改动）
    api_key_test:    Optional[str] = None
    api_secret_test: Optional[str] = None
    api_key_main:    Optional[str] = None
    api_secret_main: Optional[str] = None
    testnet: Optional[bool] = None
    symbol: Optional[str] = None
    interval: Optional[str] = None
    proxy_url: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        symbol = value.strip().upper()
        if not 2 <= len(symbol) <= 20 or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol must be 2-20 Binance symbol characters")
        return symbol


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
    return _settings_out(s)


def _settings_out(s) -> SettingsOut:
    connected = (
        app_state.client is not None
        and binance_ws_client.is_ready()
        and binance_ws_client.symbol == s.symbol
        and binance_ws_client.testnet == s.testnet
        and binance_ws_client.proxy == (s.proxy_url or None)
    )
    return SettingsOut(
        test_key_set=bool(s.api_key_test_enc or (s.testnet and s.api_key_enc)),
        main_key_set=bool(s.api_key_main_enc),
        testnet=s.testnet,
        symbol=s.symbol,
        interval=s.interval,
        proxy_url=s.proxy_url or "",
        connected=connected,
    )


def _runtime_snapshot() -> dict:
    return {
        "client": app_state.client,
        "app_symbol": app_state.symbol,
        "ws_running": getattr(binance_ws_client, "_running", False),
        "ws_symbol": binance_ws_client.symbol,
        "ws_testnet": binance_ws_client.testnet,
        "ws_proxy": binance_ws_client.proxy,
    }


def _runtime_changed(snapshot: dict) -> bool:
    return (
        app_state.client is not snapshot["client"]
        or app_state.symbol != snapshot["app_symbol"]
        or getattr(binance_ws_client, "_running", False) != snapshot["ws_running"]
        or binance_ws_client.symbol != snapshot["ws_symbol"]
        or binance_ws_client.testnet != snapshot["ws_testnet"]
        or binance_ws_client.proxy != snapshot["ws_proxy"]
    )


def _ws_target_changed(snapshot: dict, symbol: str, testnet: bool, proxy_url: Optional[str]) -> bool:
    return (
        snapshot["ws_symbol"] != symbol
        or snapshot["ws_testnet"] != testnet
        or snapshot["ws_proxy"] != (proxy_url or None)
    )


async def _start_ws_and_wait(symbol: str, testnet: bool, proxy_url: Optional[str]) -> None:
    """Restart the market stream and wait for its first validated market event."""
    async with _ws_switch_lock:
        try:
            subscription_id = await binance_ws_client.start(symbol, testnet, proxy_url)
            await binance_ws_client.wait_until_ready(
                subscription_id,
                timeout=WS_READY_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            await binance_ws_client.stop()
            raise RuntimeError(f"Binance WS did not become ready within {WS_READY_TIMEOUT}s") from exc


def _strategy_runtime_running() -> bool:
    # Local import keeps settings initialization independent of the strategy stack.
    from ..services.bot_engine import bot_engine

    return bool(bot_engine.running)


def _changes_execution_binding(body: SettingsIn, current) -> bool:
    fields = body.model_fields_set
    if "testnet" in fields and body.testnet != current.testnet:
        return True
    if "symbol" in fields and body.symbol != current.symbol:
        return True
    if "proxy_url" in fields and (body.proxy_url or None) != (current.proxy_url or None):
        return True
    return any(
        field in fields and getattr(body, field) not in (None, *_KEEP)
        for field in (
            "api_key_test",
            "api_secret_test",
            "api_key_main",
            "api_secret_main",
        )
    )


async def _restore_runtime(snapshot: dict) -> None:
    try:
        if snapshot["ws_running"]:
            await _start_ws_and_wait(
                snapshot["ws_symbol"], snapshot["ws_testnet"], snapshot["ws_proxy"]
            )
        else:
            await binance_ws_client.stop()
    finally:
        binance_ws_client.symbol = snapshot["ws_symbol"]
        binance_ws_client.testnet = snapshot["ws_testnet"]
        binance_ws_client.proxy = snapshot["ws_proxy"]
        app_state.client = snapshot["client"]
        app_state.symbol = snapshot["app_symbol"]


async def _connect_active(s) -> dict:
    """Validate private account access before activating one exchange session."""
    key_enc, sec_enc = active_keys(s)
    if not key_enc:
        raise HTTPException(400, f"当前为{'测试网' if s.testnet else '真实网'}模式，但未配置对应 API Key")
    client = await asyncio.to_thread(
        _build_client, decrypt(key_enc), decrypt(sec_enc), s.testnet, s.proxy_url
    )
    account = await asyncio.to_thread(client.futures_account)
    snapshot = _runtime_snapshot()
    try:
        await _start_ws_and_wait(s.symbol, s.testnet, s.proxy_url)
        app_state.set_client(client, s.symbol)
        return await app_state.publish_account(account)
    except (Exception, asyncio.CancelledError):
        if _runtime_changed(snapshot):
            await _restore_runtime(snapshot)
        raise


@router.post("")
async def save_settings(body: SettingsIn, db: Session = Depends(get_db)):
    async with _settings_lock:
        s = db.query(Settings).first()
        runtime = _runtime_snapshot()
        if _strategy_runtime_running() and _changes_execution_binding(body, s):
            raise HTTPException(
                status_code=409,
                detail="Stop the running strategy before changing its network, symbol, proxy, or credentials",
            )
        if body.api_key_test and body.api_key_test not in _KEEP:
            s.api_key_test_enc = encrypt(body.api_key_test.strip())
        if body.api_secret_test and body.api_secret_test not in _KEEP:
            s.api_secret_test_enc = encrypt(body.api_secret_test.strip())
        if body.api_key_main and body.api_key_main not in _KEEP:
            s.api_key_main_enc = encrypt(body.api_key_main.strip())
        if body.api_secret_main and body.api_secret_main not in _KEEP:
            s.api_secret_main_enc = encrypt(body.api_secret_main.strip())
        if body.testnet is not None:
            s.testnet = body.testnet
        if body.symbol is not None:
            s.symbol = body.symbol
        if body.interval is not None:
            s.interval = body.interval
        if "proxy_url" in body.model_fields_set:
            s.proxy_url = body.proxy_url or None

        try:
            key_enc, _ = active_keys(s)
            account = None
            if key_enc:
                account = await _connect_active(s)
            elif runtime["ws_running"] and _ws_target_changed(
                runtime, s.symbol, s.testnet, s.proxy_url
            ):
                await _start_ws_and_wait(s.symbol, s.testnet, s.proxy_url)
            db.commit()
            if not key_enc:
                app_state.symbol = s.symbol
            message = (
                f"保存成功，已连接 Binance（{'测试网' if s.testnet else '真实网'}）！"
                if key_enc
                else "配置已保存（当前模式未配置 API Key，暂未连接）"
            )
            authoritative = _settings_out(s).model_dump()
            return {"ok": True, "message": message, "account": account, **authoritative}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            try:
                if _runtime_changed(runtime):
                    await _restore_runtime(runtime)
            except Exception as restore_exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"设置保存失败且运行状态恢复失败: {restore_exc}",
                ) from exc
            if isinstance(exc, HTTPException):
                raise exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(status_code=400, detail=f"API 连接失败: {exc}") from exc


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
        client = await asyncio.to_thread(
            _build_client, decrypt(key_enc), decrypt(sec_enc), testnet, s.proxy_url
        )
        await asyncio.to_thread(client.futures_account)
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
    return rewrite_proxy_for_runtime(url)


def _build_client(api_key: str, api_secret: str, testnet: bool,
                  proxy_url: str | None = None) -> Client:
    import time as _time
    requests_params: dict = {"timeout": CONNECT_TIMEOUT}
    if proxy_url and proxy_url.strip():
        p = _rewrite_proxy(proxy_url.strip())
        requests_params["proxies"] = {"http": p, "https": p}

    client = _FuturesClient(
        api_key.strip(),
        api_secret.strip(),
        requests_params=requests_params,
    )

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
