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
from ..services.binance_errors import BinanceGatewayError
from ..services.binance_usdm_gateway import (
    BinanceUsdMGateway,
    gateway_error_detail,
    gateway_error_status,
)
from ..services.exchange_provider import (
    BINANCE_PROVIDER,
    ExchangeProvider,
    is_binance_provider,
    normalize_exchange_provider,
    unavailable_provider_detail,
)
from ..services.market_agent import market_agent_manager
import asyncio
import re
import requests as _requests

router = APIRouter()

CONNECT_TIMEOUT = 30  # 秒
WS_READY_TIMEOUT = 10  # Must fail before the frontend's 15s settings timeout.
USD_M_DEMO_REST_URL = "https://demo-fapi.binance.com/fapi"
CONNECTION_FAILURE_DETAIL = {
    "code": "binance_connection_failed",
    "message": "Binance 连接校验失败，请检查凭据、网络和代理配置。",
    "retryable": False,
}
RESTORE_FAILURE_DETAIL = {
    "code": "settings_restore_failed",
    "message": "设置保存失败，且无法恢复之前的运行状态。",
    "retryable": False,
}
IP_LOOKUP_FAILURE_DETAIL = {
    "code": "exit_ip_lookup_failed",
    "message": "后端出口 IP 检测失败。",
    "retryable": False,
}

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
    exchange_provider: Optional[ExchangeProvider] = None

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
    exchange_provider: ExchangeProvider


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    return _settings_out(s)


def _settings_out(s) -> SettingsOut:
    provider = normalize_exchange_provider(getattr(s, "exchange_provider", None))
    connected = (
        provider == BINANCE_PROVIDER
        and app_state.exchange_provider == BINANCE_PROVIDER
        and app_state.client is not None
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
        exchange_provider=provider,
    )


def _runtime_snapshot() -> dict:
    return {
        "client": app_state.client,
        "app_symbol": app_state.symbol,
        "exchange_provider": app_state.exchange_provider,
        "ws_running": getattr(binance_ws_client, "_running", False),
        "ws_symbol": binance_ws_client.symbol,
        "ws_testnet": binance_ws_client.testnet,
        "ws_proxy": binance_ws_client.proxy,
        "market_agent": market_agent_manager.status(),
    }


def _runtime_changed(snapshot: dict) -> bool:
    return (
        app_state.client is not snapshot["client"]
        or app_state.symbol != snapshot["app_symbol"]
        or app_state.exchange_provider != snapshot["exchange_provider"]
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
    """Fail closed unless the execution engine is fully stopped and released."""

    # Local import keeps settings initialization independent of the strategy stack.
    from ..services.bot_engine import bot_engine

    return bool(
        bot_engine.running
        or bot_engine.engine_state != "stopped"
        or bot_engine._task is not None
        or bot_engine._sar_adx_runtime is not None
    )


def _changes_execution_binding(body: SettingsIn, current) -> bool:
    fields = body.model_fields_set
    if (
        "exchange_provider" in fields
        and body.exchange_provider
        != normalize_exchange_provider(getattr(current, "exchange_provider", None))
    ):
        return True
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
        app_state.exchange_provider = snapshot["exchange_provider"]
    agent = snapshot.get("market_agent") or {}
    if (
        snapshot.get("market_agent_was_stopped")
        and agent.get("desired_enabled")
        and snapshot["exchange_provider"] == BINANCE_PROVIDER
        and snapshot["client"] is not None
        and agent.get("symbol")
    ):
        await market_agent_manager.start(symbol=agent["symbol"])


async def _disconnect_binance_runtime(provider: str, symbol: str) -> None:
    await market_agent_manager.stop()
    await binance_ws_client.stop()
    app_state.disconnect_exchange(provider, symbol)


async def _connect_active(s) -> dict:
    """Validate private account access before activating one exchange session."""
    if not is_binance_provider(getattr(s, "exchange_provider", None)):
        raise HTTPException(
            status_code=503,
            detail=unavailable_provider_detail(s.exchange_provider),
        )
    key_enc, sec_enc = active_keys(s)
    if not key_enc:
        raise HTTPException(400, f"当前为{'测试网' if s.testnet else '真实网'}模式，但未配置对应 API Key")
    client = await asyncio.to_thread(
        _build_client, decrypt(key_enc), decrypt(sec_enc), s.testnet, s.proxy_url
    )
    account = await asyncio.to_thread(BinanceUsdMGateway(client).account)
    snapshot = _runtime_snapshot()
    try:
        await _start_ws_and_wait(s.symbol, s.testnet, s.proxy_url)
        app_state.set_client(client, s.symbol)
        app_state.exchange_provider = BINANCE_PROVIDER
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
        previous_provider = normalize_exchange_provider(
            getattr(s, "exchange_provider", None)
        )
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
        if body.exchange_provider is not None:
            s.exchange_provider = body.exchange_provider

        try:
            provider = normalize_exchange_provider(
                getattr(s, "exchange_provider", None)
            )
            key_enc, _ = active_keys(s)
            account = None
            if provider != BINANCE_PROVIDER:
                runtime["market_agent_was_stopped"] = bool(
                    runtime["market_agent"].get("desired_enabled")
                )
                await _disconnect_binance_runtime(provider, s.symbol)
            elif key_enc:
                account = await _connect_active(s)
            elif runtime["ws_running"] and _ws_target_changed(
                runtime, s.symbol, s.testnet, s.proxy_url
            ):
                await _start_ws_and_wait(s.symbol, s.testnet, s.proxy_url)
            db.commit()
            if not key_enc:
                app_state.symbol = s.symbol
                app_state.exchange_provider = provider
            message = (
                f"保存成功，已连接 Binance（{'测试网' if s.testnet else '真实网'}）！"
                if key_enc
                else "配置已保存（当前模式未配置 API Key，暂未连接）"
            )
            if provider != BINANCE_PROVIDER:
                message = "设置已保存，所选市场将在未来接入。"
            authoritative = _settings_out(s).model_dump()
            return {"ok": True, "message": message, "account": account, **authoritative}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            s.exchange_provider = previous_provider
            try:
                if _runtime_changed(runtime) or runtime.get("market_agent_was_stopped"):
                    await _restore_runtime(runtime)
            except Exception:
                raise HTTPException(
                    status_code=500,
                    detail=RESTORE_FAILURE_DETAIL,
                ) from exc
            if isinstance(exc, HTTPException):
                raise exc
            if isinstance(exc, BinanceGatewayError):
                raise HTTPException(
                    status_code=gateway_error_status(exc),
                    detail=gateway_error_detail(exc),
                ) from exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(status_code=400, detail=CONNECTION_FAILURE_DETAIL) from exc


@router.post("/test-connection")
async def test_connection(testnet: bool = True, db: Session = Depends(get_db)):
    """独立测试测试网或真实网的 API Key 是否可用（不切换当前活跃连接）。"""
    s = db.query(Settings).first()
    provider = normalize_exchange_provider(getattr(s, "exchange_provider", None))
    if provider != BINANCE_PROVIDER:
        raise HTTPException(
            status_code=503,
            detail=unavailable_provider_detail(provider),
        )
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
        await asyncio.to_thread(BinanceUsdMGateway(client).account)
        return {"ok": True, "message": f"{net_name} 连接成功"}
    except asyncio.CancelledError:
        raise
    except BinanceGatewayError as exc:
        raise HTTPException(
            status_code=gateway_error_status(exc),
            detail=gateway_error_detail(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=CONNECTION_FAILURE_DETAIL,
        ) from exc


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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=IP_LOOKUP_FAILURE_DETAIL,
        ) from exc


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
        testnet=testnet,
    )
    if testnet:
        client.FUTURES_TESTNET_URL = USD_M_DEMO_REST_URL

    # 自动同步时间偏移，解决 -1021 timestamp 错误
    try:
        server_ts = BinanceUsdMGateway(client).server_time()
        client.timestamp_offset = server_ts - int(_time.time() * 1000)
    except Exception:
        pass

    BinanceUsdMGateway(client).ping()
    return client
