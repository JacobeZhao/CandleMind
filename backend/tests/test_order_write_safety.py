import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routes import orders
from backend.app.state import app_state


class ReadOnlyBinanceClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def futures_get_open_orders(self, **kwargs):
        self.calls.append(("open", kwargs))
        return [{"orderId": 1}]

    def futures_get_all_orders(self, **kwargs):
        self.calls.append(("history", kwargs))
        return [{"orderId": 1}, {"orderId": 2}]

    def futures_account_trades(self, **kwargs):
        self.calls.append(("trades", kwargs))
        return [{"id": 3}]


@pytest.fixture
def order_api(monkeypatch):
    fake_client = ReadOnlyBinanceClient()
    monkeypatch.setattr(app_state, "client", fake_client)
    monkeypatch.setattr(app_state, "symbol", "BTCUSDT")
    test_app = FastAPI()
    test_app.include_router(orders.router, prefix="/api/orders")
    return TestClient(test_app), fake_client


def test_order_api_exposes_only_read_operations(order_api) -> None:
    client, _ = order_api
    routes = {
        (route.path, frozenset(route.methods or set()))
        for route in client.app.routes
        if route.path.startswith("/api/orders")
    }

    assert routes == {
        ("/api/orders/open", frozenset({"GET"})),
        ("/api/orders/history", frozenset({"GET"})),
        ("/api/orders/trades", frozenset({"GET"})),
    }
    assert client.delete("/api/orders/cancel/SOLUSDT/42").status_code == 404


def test_read_operations_forward_validated_parameters(order_api) -> None:
    client, fake_client = order_api

    assert client.get("/api/orders/open", params={"symbol": "SOLUSDT"}).json() == [
        {"orderId": 1}
    ]
    assert client.get("/api/orders/history", params={"limit": 20}).json() == [
        {"orderId": 2},
        {"orderId": 1},
    ]
    assert client.get(
        "/api/orders/trades", params={"symbol": "ETHUSDT", "limit": 1000}
    ).json() == [{"id": 3}]
    assert fake_client.calls == [
        ("open", {"symbol": "SOLUSDT"}),
        ("history", {"symbol": "BTCUSDT", "limit": 20}),
        ("trades", {"symbol": "ETHUSDT", "limit": 1000}),
    ]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/orders/open", {"symbol": "solusdt"}),
        ("/api/orders/open", {"symbol": "../SOLUSDT"}),
        ("/api/orders/history", {"limit": 0}),
        ("/api/orders/history", {"limit": 1001}),
        ("/api/orders/trades", {"symbol": "BTC-USDT"}),
    ],
)
def test_invalid_order_query_is_rejected_before_binance_call(
    order_api, path: str, params: dict
) -> None:
    client, fake_client = order_api

    assert client.get(path, params=params).status_code == 422
    assert fake_client.calls == []
