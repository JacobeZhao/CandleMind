import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routes import settings as settings_routes
from backend.app.routes.settings import SettingsIn


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


def test_futures_client_skips_spot_ping_and_validates_selected_endpoint(monkeypatch):
    events = []

    monkeypatch.setattr(
        settings_routes._FuturesClient,
        "futures_time",
        lambda self: events.append(("time", self.FUTURES_URL)) or {"serverTime": 1},
    )
    monkeypatch.setattr(
        settings_routes._FuturesClient,
        "futures_ping",
        lambda self: events.append(("ping", self.FUTURES_URL)) or {},
    )

    client = settings_routes._build_client("key", "secret", testnet=True)

    assert client.FUTURES_URL == "https://testnet.binancefuture.com/fapi"
    assert events == [
        ("time", "https://testnet.binancefuture.com/fapi"),
        ("ping", "https://testnet.binancefuture.com/fapi"),
    ]


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

    with pytest.raises(HTTPException, match="offline"):
        asyncio.run(
            settings_routes.save_settings(SettingsIn(symbol="ETHUSDT"), db=db)
        )

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

    with pytest.raises(HTTPException, match="disk full"):
        asyncio.run(
            settings_routes.save_settings(SettingsIn(symbol="ETHUSDT"), db=db)
        )

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
