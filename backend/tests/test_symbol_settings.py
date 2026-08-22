import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routes import settings as settings_routes
from backend.app.routes.settings import SettingsIn
from backend.app.services.binance_errors import BinanceGatewayRejected


def _settings(**overrides):
    values = {
        "api_key_test_enc": None,
        "api_secret_test_enc": None,
        "api_key_main_enc": None,
        "api_secret_main_enc": None,
        "api_key_enc": None,
        "api_secret_enc": None,
        "testnet": True,
        "symbol": "BTCUSDT",
        "interval": "15m",
        "proxy_url": None,
        "exchange_provider": "binance",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Query:
    def __init__(self, settings):
        self.settings = settings

    def first(self):
        return self.settings


class _Db:
    def __init__(self, settings, events=None, commit_error=None):
        self.settings = settings
        self.events = events if events is not None else []
        self.commit_error = commit_error
        self.committed = False
        self.rolled_back = False

    def query(self, _model):
        return _Query(self.settings)

    def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise self.commit_error
        self.committed = True

    def rollback(self):
        self.events.append("rollback")
        self.rolled_back = True


def _set_runtime(monkeypatch, *, symbol="BTCUSDT", running=False):
    client = object()
    monkeypatch.setattr(settings_routes.app_state, "client", client)
    monkeypatch.setattr(settings_routes.app_state, "symbol", symbol)
    monkeypatch.setattr(settings_routes.app_state, "exchange_provider", "binance")
    monkeypatch.setattr(settings_routes.binance_ws_client, "_running", running)
    monkeypatch.setattr(settings_routes.binance_ws_client, "symbol", symbol)
    monkeypatch.setattr(settings_routes.binance_ws_client, "testnet", True)
    monkeypatch.setattr(settings_routes.binance_ws_client, "proxy", None)
    monkeypatch.setattr(
        settings_routes.binance_ws_client,
        "is_ready",
        lambda: running,
    )
    return client


def test_symbol_is_normalized_and_invalid_formats_are_rejected():
    assert SettingsIn(symbol="  btcusdt  ").symbol == "BTCUSDT"
    assert SettingsIn(symbol="btcusdt_250926").symbol == "BTCUSDT_250926"

    for invalid in ("", "B", "BTC/USDT", "BTC-USDT", "_BTCUSDT", "BTCUSDT_", "A" * 21):
        with pytest.raises(ValidationError):
            SettingsIn(symbol=invalid)


def test_exchange_provider_contract_accepts_only_supported_values():
    for provider in ("binance", "okx", "bybit", "gateio", "a_share"):
        assert SettingsIn(exchange_provider=provider).exchange_provider == provider

    with pytest.raises(ValidationError):
        SettingsIn(exchange_provider="coinbase")


def test_futures_client_skips_spot_ping_and_validates_selected_endpoint(monkeypatch):
    events = []

    monkeypatch.setattr(
        settings_routes._FuturesClient,
        "futures_time",
        lambda self: events.append(("time", self._create_futures_api_uri("time")))
        or {"serverTime": 1},
    )
    monkeypatch.setattr(
        settings_routes._FuturesClient,
        "futures_ping",
        lambda self: events.append(("ping", self._create_futures_api_uri("ping")))
        or {},
    )

    client = settings_routes._build_client("key", "secret", testnet=True)

    assert client.testnet is True
    assert client._create_futures_api_uri("ping") == (
        "https://demo-fapi.binance.com/fapi/v1/ping"
    )
    assert events == [
        ("time", "https://demo-fapi.binance.com/fapi/v1/time"),
        ("ping", "https://demo-fapi.binance.com/fapi/v1/ping"),
    ]


def test_connect_active_requires_private_account_access_before_switch(monkeypatch):
    original_client = _set_runtime(monkeypatch, running=True)
    current = _settings(
        api_key_main_enc="encrypted-key",
        api_secret_main_enc="encrypted-secret",
        testnet=False,
    )
    events = []

    class Client:
        def futures_account(self):
            events.append("private-account")
            raise RuntimeError("Invalid API-key, IP, or permissions")

    monkeypatch.setattr(settings_routes, "decrypt", lambda value: value)
    monkeypatch.setattr(settings_routes, "_build_client", lambda *_args: Client())

    async def must_not_start(*_args):
        raise AssertionError("market stream must not switch after private auth failure")

    monkeypatch.setattr(settings_routes, "_start_ws_and_wait", must_not_start)

    with pytest.raises(BinanceGatewayRejected) as error:
        asyncio.run(settings_routes._connect_active(current))

    assert str(error.value) == "Binance rejected the request"
    assert "Invalid API-key" not in str(error.value)
    assert events == ["private-account"]
    assert settings_routes.app_state.client is original_client
    assert settings_routes.binance_ws_client.testnet is True


def test_connect_active_returns_and_publishes_validated_account(monkeypatch):
    _set_runtime(monkeypatch)
    current = _settings(
        api_key_main_enc="encrypted-key",
        api_secret_main_enc="encrypted-secret",
        testnet=False,
        symbol="SOLUSDT",
    )
    account = {
        "totalWalletBalance": "123.45",
        "assets": [{"asset": "USDT", "availableBalance": "100.00"}],
    }
    client = SimpleNamespace(futures_account=lambda: account)
    published = []

    monkeypatch.setattr(settings_routes, "decrypt", lambda value: value)
    monkeypatch.setattr(settings_routes, "_build_client", lambda *_args: client)

    async def start(symbol, testnet, proxy):
        settings_routes.binance_ws_client.symbol = symbol
        settings_routes.binance_ws_client.testnet = testnet
        settings_routes.binance_ws_client.proxy = proxy

    async def publish(value):
        published.append(value)
        return {"totalWalletBalance": value["totalWalletBalance"]}

    monkeypatch.setattr(settings_routes, "_start_ws_and_wait", start)
    monkeypatch.setattr(settings_routes.app_state, "set_client", lambda value, symbol: events.append((value, symbol)))
    monkeypatch.setattr(settings_routes.app_state, "publish_account", publish)
    events = []

    result = asyncio.run(settings_routes._connect_active(current))

    assert events == [(client, "SOLUSDT")]
    assert published == [account]
    assert result == {"totalWalletBalance": "123.45"}


def test_save_without_api_key_commits_normalized_symbol_to_app_state(monkeypatch):
    original_client = _set_runtime(monkeypatch)
    current = _settings()
    db = _Db(current)

    result = asyncio.run(
        settings_routes.save_settings(SettingsIn(symbol=" ethusdt "), db=db)
    )

    assert db.committed
    assert not db.rolled_back
    assert current.symbol == "ETHUSDT"
    assert settings_routes.app_state.symbol == "ETHUSDT"
    assert settings_routes.app_state.client is original_client
    assert result["symbol"] == "ETHUSDT"


def test_save_with_api_key_waits_for_connection_before_commit(monkeypatch):
    _set_runtime(monkeypatch)
    current = _settings(api_key_test_enc="encrypted-key")
    events = []
    db = _Db(current, events)

    async def connect_active(settings):
        events.append(f"connected:{settings.symbol}")
        settings_routes.app_state.symbol = settings.symbol
        settings_routes.binance_ws_client.symbol = settings.symbol

    monkeypatch.setattr(settings_routes, "_connect_active", connect_active)

    result = asyncio.run(
        settings_routes.save_settings(SettingsIn(symbol="ethusdt"), db=db)
    )

    assert events == ["connected:ETHUSDT", "commit"]
    assert result["symbol"] == "ETHUSDT"


def test_empty_save_reconnects_with_existing_settings_before_commit(monkeypatch):
    _set_runtime(monkeypatch)
    current = _settings(
        api_key_test_enc="encrypted-key",
        api_secret_test_enc="encrypted-secret",
        interval="5m",
        proxy_url="http://proxy.test:8080",
    )
    original = vars(current).copy()
    events = []
    db = _Db(current, events)

    async def connect_active(settings):
        assert settings is current
        events.append(f"connected:{settings.symbol}")

    monkeypatch.setattr(settings_routes, "_connect_active", connect_active)

    result = asyncio.run(settings_routes.save_settings(SettingsIn(), db=db))

    assert events == ["connected:BTCUSDT", "commit"]
    assert db.committed
    assert not db.rolled_back
    assert vars(current) == original
    assert result["ok"] is True
    assert result["symbol"] == "BTCUSDT"


def test_connection_failure_rolls_back_database_and_runtime(monkeypatch):
    original_client = _set_runtime(monkeypatch)
    current = _settings(api_key_test_enc="encrypted-key")
    db = _Db(current)
    restored = []

    async def fail_after_switch(settings):
        settings_routes.app_state.client = object()
        settings_routes.app_state.symbol = settings.symbol
        settings_routes.binance_ws_client.symbol = settings.symbol
        raise OSError("offline")

    async def restore(snapshot):
        restored.append(snapshot)
        settings_routes.app_state.client = snapshot["client"]
        settings_routes.app_state.symbol = snapshot["app_symbol"]
        settings_routes.binance_ws_client.symbol = snapshot["ws_symbol"]

    monkeypatch.setattr(settings_routes, "_connect_active", fail_after_switch)
    monkeypatch.setattr(settings_routes, "_restore_runtime", restore)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            settings_routes.save_settings(SettingsIn(symbol="ETHUSDT"), db=db)
        )

    assert error.value.detail == settings_routes.CONNECTION_FAILURE_DETAIL
    assert "offline" not in error.value.detail
    assert db.rolled_back
    assert not db.committed
    assert len(restored) == 1
    assert settings_routes.app_state.client is original_client
    assert settings_routes.app_state.symbol == "BTCUSDT"
    assert settings_routes.binance_ws_client.symbol == "BTCUSDT"


def test_commit_failure_restores_connected_runtime(monkeypatch):
    _set_runtime(monkeypatch)
    current = _settings(api_key_test_enc="encrypted-key")
    db = _Db(current, commit_error=OSError("disk full"))
    restored = []

    async def connect_active(settings):
        settings_routes.app_state.client = object()
        settings_routes.app_state.symbol = settings.symbol
        settings_routes.binance_ws_client.symbol = settings.symbol

    async def restore(snapshot):
        restored.append(snapshot)
        settings_routes.app_state.client = snapshot["client"]
        settings_routes.app_state.symbol = snapshot["app_symbol"]
        settings_routes.binance_ws_client.symbol = snapshot["ws_symbol"]

    monkeypatch.setattr(settings_routes, "_connect_active", connect_active)
    monkeypatch.setattr(settings_routes, "_restore_runtime", restore)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            settings_routes.save_settings(SettingsIn(symbol="ETHUSDT"), db=db)
        )

    assert error.value.detail == settings_routes.CONNECTION_FAILURE_DETAIL
    assert "disk full" not in error.value.detail
    assert db.rolled_back
    assert len(restored) == 1
    assert settings_routes.app_state.symbol == "BTCUSDT"
    assert settings_routes.binance_ws_client.symbol == "BTCUSDT"


def test_ws_restart_waits_for_target_ticker(monkeypatch):
    _set_runtime(monkeypatch)
    events = []

    async def start(symbol, testnet, proxy):
        events.append("start")
        settings_routes.binance_ws_client.symbol = symbol
        settings_routes.binance_ws_client.testnet = testnet
        settings_routes.binance_ws_client.proxy = proxy
        settings_routes.binance_ws_client._running = True
        return 42

    async def wait_until_ready(subscription_id, timeout):
        events.append(("ready", subscription_id, timeout))

    monkeypatch.setattr(settings_routes.binance_ws_client, "start", start)
    monkeypatch.setattr(
        settings_routes.binance_ws_client,
        "wait_until_ready",
        wait_until_ready,
    )

    asyncio.run(settings_routes._start_ws_and_wait("ETHUSDT", False, "http://proxy"))

    assert events == ["start", ("ready", 42, settings_routes.WS_READY_TIMEOUT)]


def test_save_returns_authoritative_network_and_connection_state(monkeypatch):
    _set_runtime(monkeypatch, running=True)
    current = _settings(testnet=True, symbol="BTCUSDT")
    db = _Db(current)

    result = asyncio.run(settings_routes.save_settings(SettingsIn(), db=db))

    assert result["testnet"] is True
    assert result["symbol"] == "BTCUSDT"
    assert result["connected"] is True
    assert result["test_key_set"] is False


@pytest.mark.parametrize(
    "body",
    [
        SettingsIn(testnet=False),
        SettingsIn(symbol="ETHUSDT"),
        SettingsIn(proxy_url="http://proxy.test:8080"),
        SettingsIn(api_key_test="replacement"),
        SettingsIn(exchange_provider="okx"),
    ],
)
def test_running_strategy_rejects_execution_binding_changes(monkeypatch, body):
    _set_runtime(monkeypatch)
    current = _settings()
    db = _Db(current)
    monkeypatch.setattr(settings_routes, "_strategy_runtime_running", lambda: True)

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes.save_settings(body, db=db))

    assert error.value.status_code == 409
    assert not db.committed
    assert vars(current) == vars(_settings())


def test_running_strategy_allows_non_binding_interval_change(monkeypatch):
    _set_runtime(monkeypatch)
    current = _settings()
    db = _Db(current)
    monkeypatch.setattr(settings_routes, "_strategy_runtime_running", lambda: True)

    result = asyncio.run(
        settings_routes.save_settings(SettingsIn(interval="5m"), db=db)
    )

    assert db.committed
    assert current.interval == "5m"
    assert result["interval"] == "5m"


@pytest.mark.parametrize(
    ("running", "engine_state", "task", "runtime"),
    [
        (True, "running", None, None),
        (False, "recovery_required", None, None),
        (False, "stopped", object(), None),
        (False, "stopped", None, object()),
    ],
)
def test_execution_binding_change_is_blocked_until_runtime_is_fully_released(
    monkeypatch, running, engine_state, task, runtime
):
    from backend.app.services.bot_engine import bot_engine

    monkeypatch.setattr(bot_engine, "running", running)
    monkeypatch.setattr(bot_engine, "engine_state", engine_state)
    monkeypatch.setattr(bot_engine, "_task", task)
    monkeypatch.setattr(bot_engine, "_sar_adx_runtime", runtime)

    assert settings_routes._strategy_runtime_running() is True


def test_execution_binding_change_is_allowed_for_fully_stopped_runtime(monkeypatch):
    from backend.app.services.bot_engine import bot_engine

    monkeypatch.setattr(bot_engine, "running", False)
    monkeypatch.setattr(bot_engine, "engine_state", "stopped")
    monkeypatch.setattr(bot_engine, "_task", None)
    monkeypatch.setattr(bot_engine, "_sar_adx_runtime", None)

    assert settings_routes._strategy_runtime_running() is False


def test_restore_failure_still_restores_symbol_metadata(monkeypatch):
    original_client = _set_runtime(monkeypatch, symbol="BTCUSDT", running=True)
    snapshot = settings_routes._runtime_snapshot()
    settings_routes.app_state.client = object()
    settings_routes.app_state.symbol = "ETHUSDT"
    settings_routes.binance_ws_client.symbol = "ETHUSDT"
    settings_routes.binance_ws_client.testnet = False

    async def fail_restore(*_args):
        raise OSError("still offline")

    monkeypatch.setattr(settings_routes, "_start_ws_and_wait", fail_restore)

    with pytest.raises(OSError, match="still offline"):
        asyncio.run(settings_routes._restore_runtime(snapshot))

    assert settings_routes.app_state.client is original_client
    assert settings_routes.app_state.symbol == "BTCUSDT"
    assert settings_routes.binance_ws_client.symbol == "BTCUSDT"
    assert settings_routes.binance_ws_client.testnet is True


def test_connection_diagnostics_do_not_expose_exception_or_proxy_secrets(monkeypatch):
    secret = "proxy-user:proxy-password"
    current = _settings(
        api_key_test_enc="encrypted-key",
        api_secret_test_enc="encrypted-secret",
        proxy_url=f"http://{secret}@proxy.test:8080",
    )
    db = _Db(current)

    monkeypatch.setattr(settings_routes, "decrypt", lambda value: value)
    monkeypatch.setattr(
        settings_routes,
        "_build_client",
        lambda *_args: (_ for _ in ()).throw(OSError(f"failed via {secret}")),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes.test_connection(testnet=True, db=db))

    assert error.value.detail == settings_routes.CONNECTION_FAILURE_DETAIL
    assert secret not in str(error.value.detail)


def test_exit_ip_failure_is_sanitized(monkeypatch):
    secret = "proxy-user:proxy-password"
    db = _Db(_settings(proxy_url=f"http://{secret}@proxy.test:8080"))
    monkeypatch.setattr(
        settings_routes._requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(f"proxy rejected {secret}")
        ),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes.get_my_ip(db=db))

    assert error.value.detail == settings_routes.IP_LOOKUP_FAILURE_DETAIL
    assert secret not in str(error.value.detail)


def test_switch_to_unavailable_provider_stops_binance_runtime(monkeypatch):
    original_client = _set_runtime(monkeypatch, running=True)
    current = _settings(api_key_test_enc="encrypted-key")
    db = _Db(current)
    events = []

    async def stop_agent():
        events.append("agent-stopped")

    async def stop_ws():
        events.append("ws-stopped")
        settings_routes.binance_ws_client._running = False

    monkeypatch.setattr(settings_routes.market_agent_manager, "stop", stop_agent)
    monkeypatch.setattr(settings_routes.binance_ws_client, "stop", stop_ws)
    monkeypatch.setattr(
        settings_routes,
        "_connect_active",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Binance must not connect")),
    )

    result = asyncio.run(
        settings_routes.save_settings(SettingsIn(exchange_provider="okx"), db=db)
    )

    assert original_client is not None
    assert events == ["agent-stopped", "ws-stopped"]
    assert settings_routes.app_state.client is None
    assert settings_routes.app_state.exchange_provider == "okx"
    assert result["exchange_provider"] == "okx"
    assert result["connected"] is False


def test_unavailable_provider_commit_failure_restores_binance_runtime(monkeypatch):
    original_client = _set_runtime(monkeypatch, running=True)
    current = _settings(api_key_test_enc="encrypted-key")
    db = _Db(current, commit_error=OSError("disk full"))
    restored = []

    monkeypatch.setattr(
        settings_routes.market_agent_manager,
        "stop",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        settings_routes.binance_ws_client,
        "stop",
        lambda: asyncio.sleep(0),
    )

    async def restore(snapshot):
        restored.append(snapshot)
        settings_routes.app_state.client = snapshot["client"]
        settings_routes.app_state.symbol = snapshot["app_symbol"]
        settings_routes.app_state.exchange_provider = snapshot["exchange_provider"]

    monkeypatch.setattr(settings_routes, "_restore_runtime", restore)

    with pytest.raises(HTTPException):
        asyncio.run(
            settings_routes.save_settings(
                SettingsIn(exchange_provider="bybit"), db=db
            )
        )

    assert len(restored) == 1
    assert current.exchange_provider == "binance"
    assert settings_routes.app_state.client is original_client
    assert settings_routes.app_state.exchange_provider == "binance"


def test_connection_test_rejects_unavailable_provider_without_building_client(monkeypatch):
    db = _Db(_settings(exchange_provider="gateio", api_key_test_enc="key"))
    monkeypatch.setattr(
        settings_routes,
        "_build_client",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Binance must not connect")),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(settings_routes.test_connection(testnet=True, db=db))

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "exchange_provider_unavailable"
    assert error.value.detail["provider"] == "gateio"
