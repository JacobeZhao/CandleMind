from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.routes import strategy_analytics as routes


class _Query:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Db:
    def __init__(self, settings):
        self.settings = settings

    def query(self, model):
        assert model is routes.Settings
        return _Query(self.settings)


def test_router_exposes_only_backend_scoped_paths():
    paths = {(route.path, tuple(sorted(route.methods))) for route in routes.router.routes}
    assert ("/api/strategy/analytics", ("GET",)) in paths
    assert ("/api/strategy/analytics/sync", ("POST",)) in paths


def test_active_scope_uses_db_network_symbol_and_decrypted_key(monkeypatch):
    settings = SimpleNamespace(testnet=False, symbol="ETHUSDT")
    monkeypatch.setattr(routes.app_state, "client", SimpleNamespace(API_KEY="active-api-key"))
    monkeypatch.setattr(routes.app_state, "symbol", "ETHUSDT")
    monkeypatch.setattr(routes, "active_keys", lambda value: ("encrypted", "secret"))
    monkeypatch.setattr(routes, "decrypt", lambda value: "active-api-key")

    client, fingerprint, network, symbol = routes._active_scope(_Db(settings))

    assert client is routes.app_state.client
    assert network == "mainnet"
    assert symbol == "ETHUSDT"
    assert "active-api-key" not in fingerprint


def test_get_returns_stale_snapshot_with_sanitized_refresh_failure(monkeypatch):
    settings = SimpleNamespace(testnet=True, symbol="SOLUSDT")
    monkeypatch.setattr(routes.app_state, "client", SimpleNamespace(API_KEY="key"))
    monkeypatch.setattr(routes.app_state, "symbol", "SOLUSDT")
    monkeypatch.setattr(routes, "active_keys", lambda value: ("encrypted", "secret"))
    monkeypatch.setattr(routes, "decrypt", lambda value: "key")

    class Service:
        store = SimpleNamespace(ensure_scope=lambda *args: 3)

        def sync(self, *args):
            raise RuntimeError("apiKey=private-value")

        def snapshot(self, scope_id):
            return {"schema_version": 1, "scope_id": scope_id}

    monkeypatch.setattr(routes, "analytics_service", Service())
    result = asyncio.run(routes.get_strategy_analytics(_Db(settings)))

    assert result["stale"] is True
    assert result["refresh"] == {"status": "failed", "reasons": ["refresh_failed"]}
    assert "private-value" not in str(result)


def test_scope_rejects_disconnected_backend():
    original = routes.app_state.client
    routes.app_state.client = None
    try:
        try:
            routes._active_scope(_Db(SimpleNamespace(testnet=True, symbol="SOLUSDT")))
        except HTTPException as exc:
            assert exc.status_code == 503
        else:
            raise AssertionError("disconnected scope was accepted")
    finally:
        routes.app_state.client = original


def test_scope_rejects_client_and_committed_credential_mismatch(monkeypatch):
    settings = SimpleNamespace(testnet=True, symbol="SOLUSDT")
    monkeypatch.setattr(routes.app_state, "client", SimpleNamespace(API_KEY="new-key"))
    monkeypatch.setattr(routes.app_state, "symbol", "SOLUSDT")
    monkeypatch.setattr(routes, "active_keys", lambda value: ("encrypted", "secret"))
    monkeypatch.setattr(routes, "decrypt", lambda value: "old-key")

    try:
        routes._active_scope(_Db(settings))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("mismatched client and committed credentials were accepted")
