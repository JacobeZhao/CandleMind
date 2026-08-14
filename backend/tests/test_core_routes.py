from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.database import get_db
from backend.app.routes import account, health, orders
from backend.app.services.bot_engine import bot_engine
from backend.app.state import app_state


class FakeBinanceClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def futures_account_balance(self):
        return [
            {"asset": "USDT", "balance": "100", "availableBalance": "75"},
            {"asset": "BTC", "balance": "0", "availableBalance": "0"},
        ]

    def futures_account(self):
        return {
            "totalWalletBalance": "100",
            "totalUnrealizedProfit": "5",
            "totalMarginBalance": "105",
        }

    def futures_position_information(self):
        return [
            {"symbol": "SOLUSDT", "positionAmt": "2"},
            {"symbol": "BTCUSDT", "positionAmt": "0"},
        ]

    def futures_get_open_orders(self, **kwargs):
        self.calls.append(("open", kwargs))
        return [{"orderId": 1}]

    def futures_get_all_orders(self, **kwargs):
        self.calls.append(("history", kwargs))
        return [{"orderId": 1}, {"orderId": 2}]

    def futures_account_trades(self, **kwargs):
        self.calls.append(("trades", kwargs))
        return [{"id": 3}]

    def futures_cancel_order(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"status": "CANCELED", **kwargs}


class FakeQuery:
    def __init__(self, settings: SimpleNamespace) -> None:
        self.settings = settings

    def first(self):
        return self.settings


class FakeSession:
    def __init__(self, settings: SimpleNamespace) -> None:
        self.settings = settings

    def query(self, _model):
        return FakeQuery(self.settings)


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(health.router, prefix="/api/health")
    test_app.include_router(account.router, prefix="/api/account")
    test_app.include_router(orders.router, prefix="/api/orders")
    test_app.dependency_overrides[get_db] = lambda: FakeSession(
        SimpleNamespace(proxy_url="http://proxy.test:8080", testnet=True)
    )
    return TestClient(test_app)


@pytest.fixture(autouse=True)
def restore_runtime_state():
    original_client = app_state.client
    original_symbol = app_state.symbol
    try:
        yield
    finally:
        app_state.client = original_client
        app_state.symbol = original_symbol


def test_health_reports_runtime_and_mocked_exit_ip(client, monkeypatch):
    fake_response = SimpleNamespace(
        json=lambda: {
            "query": "203.0.113.10",
            "countryCode": "SG",
            "country": "Singapore",
        }
    )
    requests_seen = []

    def fake_get(url, **kwargs):
        requests_seen.append((url, kwargs))
        return fake_response

    monkeypatch.setattr("requests.get", fake_get)
    app_state.client = FakeBinanceClient()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "engine_running": bot_engine.running,
        "paper": bot_engine.paper,
        "testnet": True,
        "proxy_set": True,
        "exit_ip": "203.0.113.10",
        "country": "Singapore",
        "restricted": False,
    }
    assert requests_seen == [
        (
            "http://ip-api.com/json/?fields=query,countryCode,country",
            {
                "proxies": {
                    "http": "http://proxy.test:8080",
                    "https": "http://proxy.test:8080",
                },
                "timeout": 10,
            },
        )
    ]


def test_health_handles_ip_lookup_failure_without_network(client, monkeypatch):
    monkeypatch.setattr(
        "requests.get", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["exit_ip"] is None
    assert response.json()["restricted"] is None
    assert response.json()["ip_error"] == "offline"


@pytest.mark.parametrize(
    "path",
    [
        "/api/account/balance",
        "/api/account/positions",
        "/api/orders/open",
        "/api/orders/history",
        "/api/orders/trades",
        "/api/orders/cancel/SOLUSDT/42",
    ],
)
def test_account_and_order_routes_require_binance_connection(client, path):
    app_state.client = None

    response = client.delete(path) if "/cancel/" in path else client.get(path)

    assert response.status_code == 503


def test_account_routes_filter_zero_balances_and_positions(client):
    app_state.client = FakeBinanceClient()

    balance = client.get("/api/account/balance")
    positions = client.get("/api/account/positions")

    assert balance.status_code == 200
    assert balance.json() == {
        "balances": [
            {"asset": "USDT", "balance": "100", "availableBalance": "75"}
        ],
        "totalWalletBalance": "100",
        "totalUnrealizedProfit": "5",
        "totalMarginBalance": "105",
        "availableBalance": "75",
    }
    assert positions.status_code == 200
    assert positions.json() == [{"symbol": "SOLUSDT", "positionAmt": "2"}]


def test_order_routes_forward_parameters_and_reverse_history(client):
    fake = FakeBinanceClient()
    app_state.client = fake
    app_state.symbol = "SOLUSDT"

    assert client.get("/api/orders/open?symbol=BTCUSDT").json() == [{"orderId": 1}]
    assert client.get("/api/orders/history?limit=7").json() == [
        {"orderId": 2},
        {"orderId": 1},
    ]
    assert client.get("/api/orders/trades?symbol=ETHUSDT&limit=9").json() == [
        {"id": 3}
    ]
    assert client.delete("/api/orders/cancel/SOLUSDT/42").json() == {
        "status": "CANCELED",
        "symbol": "SOLUSDT",
        "orderId": 42,
    }
    assert fake.calls == [
        ("open", {"symbol": "BTCUSDT"}),
        ("history", {"symbol": "SOLUSDT", "limit": 7}),
        ("trades", {"symbol": "ETHUSDT", "limit": 9}),
        ("cancel", {"symbol": "SOLUSDT", "orderId": 42}),
    ]
