import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routes import orders
from backend.app.state import app_state


class ReadOnlyBinanceClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.testnet = False
        self.open_orders_error = None
        self.open_algo_orders_error = None
        self.account_error = None
        self.positions_error = None

    def futures_account(self):
        if self.account_error:
            raise self.account_error
        return {"assets": [], "totalWalletBalance": "10"}

    def futures_position_information(self, **_kwargs):
        if self.positions_error:
            raise self.positions_error
        return [{"symbol": "BTCUSDT", "positionAmt": "1"}]

    def futures_get_open_orders(self, **kwargs):
        self.calls.append(("open", kwargs))
        if self.open_orders_error:
            raise self.open_orders_error
        return [{"orderId": 1}]

    def futures_get_open_algo_orders(self, **kwargs):
        self.calls.append(("open-algo", kwargs))
        if self.open_algo_orders_error:
            raise self.open_algo_orders_error
        return [{
            "algoId": 2,
            "clientAlgoId": "algo-client-2",
            "orderType": "STOP_MARKET",
            "quantity": "3",
            "triggerPrice": "80",
            "algoStatus": "NEW",
            "createTime": 1234,
            "symbol": "BTCUSDT",
        }]

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
        ("/api/orders/open/combined", frozenset({"GET"})),
        ("/api/orders/analytics", frozenset({"GET"})),
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


def test_open_order_push_aggregates_regular_and_algo_orders_with_scope(monkeypatch):
    fake_client = ReadOnlyBinanceClient()
    fake_client.testnet = True
    state = type(app_state)()
    state.client = fake_client
    state.symbol = "SOLUSDT"
    messages = []

    async def capture(message):
        messages.append(message)

    monkeypatch.setattr("backend.app.state.manager.broadcast", capture)

    asyncio.run(state._push_open_orders())

    assert fake_client.calls == [
        ("open", {"symbol": "SOLUSDT"}),
        ("open-algo", {"symbol": "SOLUSDT"}),
    ]
    assert messages[0]["type"] == "open_orders"
    assert messages[0]["symbol"] == "SOLUSDT"
    assert messages[0]["network"] == "testnet"
    assert messages[0]["data"][0]["orderSource"] == "regular"
    assert messages[0]["data"][1] == {
        "algoId": 2,
        "clientAlgoId": "algo-client-2",
        "orderType": "STOP_MARKET",
        "quantity": "3",
        "triggerPrice": "80",
        "algoStatus": "NEW",
        "createTime": 1234,
        "symbol": "BTCUSDT",
        "orderId": 2,
        "clientOrderId": "algo-client-2",
        "type": "STOP_MARKET",
        "origQty": "3",
        "price": "0",
        "stopPrice": "80",
        "status": "NEW",
        "time": 1234,
        "orderSource": "algo",
    }


@pytest.mark.parametrize("failed_query", ["regular", "algo"])
def test_open_order_push_broadcasts_error_instead_of_empty_orders(
    monkeypatch, failed_query
):
    fake_client = ReadOnlyBinanceClient()
    if failed_query == "regular":
        fake_client.open_orders_error = TimeoutError("regular orders timed out")
    else:
        fake_client.open_algo_orders_error = TimeoutError("algo orders timed out")
    state = type(app_state)()
    state.client = fake_client
    state.symbol = "SOLUSDT"
    messages = []

    async def capture(message):
        messages.append(message)

    monkeypatch.setattr("backend.app.state.manager.broadcast", capture)

    asyncio.run(state._push_open_orders())

    assert messages == [{
        "type": "open_orders_error",
        "symbol": "SOLUSDT",
        "network": "mainnet",
        "data": {
                "code": "binance_unavailable",
                "message": "Binance 请求超时，服务器已完成自动重试。",
            "retryable": True,
        },
    }]
    assert all(message.get("data") != [] for message in messages)


def test_account_and_position_pushes_emit_errors_without_fake_empty_success(monkeypatch):
    fake_client = ReadOnlyBinanceClient()
    fake_client.account_error = TimeoutError("account private detail")
    fake_client.positions_error = TimeoutError("positions private detail")
    state = type(app_state)()
    state.client = fake_client
    messages = []

    async def capture(message):
        messages.append(message)

    monkeypatch.setattr("backend.app.state.manager.broadcast", capture)

    asyncio.run(state._push_account())
    asyncio.run(state._push_positions())

    assert [message["type"] for message in messages] == [
        "account_error",
        "positions_error",
    ]
    assert all(message["data"] == {
        "code": "binance_unavailable",
        "message": "Binance 请求超时，服务器已完成自动重试。",
        "retryable": True,
    } for message in messages)
    assert not any(message["type"] in {"account", "positions"} for message in messages)
