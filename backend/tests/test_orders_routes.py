from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from requests.exceptions import Timeout
from binance.exceptions import BinanceAPIException
from requests import Response

from backend.app.routes import orders
from backend.app.state import app_state


@pytest.fixture(autouse=True)
def restore_app_state():
    original_client = app_state.client
    original_symbol = app_state.symbol
    original_provider = app_state.exchange_provider
    app_state.exchange_provider = "binance"
    try:
        yield
    finally:
        app_state.client = original_client
        app_state.symbol = original_symbol
        app_state.exchange_provider = original_provider


class FakeClient:
    API_KEY = "read-only-test-key"
    testnet = False

    def __init__(self) -> None:
        self.regular_error = None
        self.algo_error = None
        self.trade_error = None

    def futures_get_open_orders(self, **_params):
        if self.regular_error:
            raise self.regular_error
        return [{
            "orderId": 7, "clientOrderId": "regular", "symbol": "SOLUSDT",
            "side": "SELL", "type": "STOP_MARKET", "status": "NEW",
            "origQty": "2", "executedQty": "0", "price": "0",
            "stopPrice": "90", "time": 100, "updateTime": 101,
            "reduceOnly": True,
        }]

    def futures_get_open_algo_orders(self, **_params):
        if self.algo_error:
            raise self.algo_error
        return [
            {
                "algoId": 8, "actualOrderId": 7, "clientAlgoId": "algo-overlap",
                "symbol": "SOLUSDT", "side": "SELL", "orderType": "STOP_MARKET",
                "algoStatus": "NEW", "quantity": "2", "triggerPrice": "90",
                "price": "0", "createTime": 100, "updateTime": 102,
                "reduceOnly": True,
            },
            {
                "algoId": 9, "actualOrderId": "", "clientAlgoId": "algo-only",
                "symbol": "SOLUSDT", "side": "BUY", "orderType": "TAKE_PROFIT",
                "algoStatus": "NEW", "quantity": "1", "triggerPrice": "110",
                "price": "111", "createTime": 200, "updateTime": 201,
            },
        ]

    def futures_account_trades(self, **params):
        if self.trade_error:
            raise self.trade_error
        now = params["endTime"]
        return [] if params["startTime"] < now else []


def _api(fake: FakeClient) -> TestClient:
    orders.account_analytics_service._cache.clear()
    app_state.client = fake
    app_state.symbol = "SOLUSDT"
    app_state.exchange_provider = "binance"
    test_app = FastAPI()
    test_app.include_router(orders.router, prefix="/api/orders")
    return TestClient(test_app)


def test_combined_open_orders_normalizes_and_deduplicates() -> None:
    response = _api(FakeClient()).get("/api/orders/open/combined?symbol=SOLUSDT")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == {"network": "mainnet", "symbol": "SOLUSDT"}
    assert payload["status"] == "complete"
    assert payload["counts"] == {"regular": 1, "algo": 2, "total": 2}
    overlap = next(row for row in payload["orders"] if row["actualOrderId"] == 7)
    assert overlap["source"] == "regular+algo"
    assert overlap["aliases"] == ["algo:8"]


def test_combined_open_orders_returns_partial_when_one_source_fails() -> None:
    fake = FakeClient()
    fake.algo_error = Timeout("private upstream detail")

    response = _api(fake).get("/api/orders/open/combined")

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["warnings"] == ["algo_orders_unavailable"]
    assert all(isinstance(warning, str) for warning in response.json()["warnings"])
    assert "private" not in response.text


def test_combined_open_orders_maps_total_failure_without_leaking_details() -> None:
    fake = FakeClient()
    fake.regular_error = Timeout("regular secret")
    fake.algo_error = Timeout("algo secret")

    response = _api(fake).get("/api/orders/open/combined")

    assert response.status_code == 503
    assert response.json() == {"detail": {
        "code": "binance_unavailable",
        "message": "Binance 请求超时，服务器已完成自动重试。",
        "retryable": True,
    }}
    assert "secret" not in response.text


def test_new_routes_reject_non_active_symbol_before_exchange_call() -> None:
    fake = FakeClient()

    response = _api(fake).get("/api/orders/analytics?symbol=BTCUSDT")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "scope_conflict",
        "message": "请求品种与当前品种不一致，请刷新后重试。",
        "retryable": True,
    }


def test_account_analytics_exposes_account_scope_and_unavailable_returns() -> None:
    fake = FakeClient()

    response = _api(fake).get("/api/orders/analytics?symbol=SOLUSDT")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["basis"] == "account"
    assert payload["counts"]["long"] == 0
    assert payload["counts"]["short"] == 0
    assert payload["week"]["net_pnl_usdt"] == "0"
    assert payload["week"]["net_return_pct"] is None
    assert payload["week"]["return_status"] == "unavailable"


def test_account_analytics_maps_transport_failure_safely() -> None:
    fake = FakeClient()
    fake.trade_error = Timeout("credential-like detail")

    response = _api(fake).get("/api/orders/analytics")

    assert response.status_code == 503
    assert response.json() == {"detail": {
        "code": "binance_unavailable",
        "message": "Binance 请求超时，服务器已完成自动重试。",
        "retryable": True,
    }}
    assert "credential-like" not in response.text


def test_history_maps_binance_ip_allowlist_rejection_to_actionable_401() -> None:
    fake = FakeClient()

    def rejected(**_params):
        response = Response()
        response.status_code = 401
        response._content = b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions"}'
        raise BinanceAPIException(response, 401, response.text)

    fake.futures_get_all_orders = rejected

    response = _api(fake).get("/api/orders/history?symbol=SOLUSDT")

    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "binance_access_rejected",
        "message": "Binance 拒绝了账户请求，请核对 API Key、USD-M 合约权限和后端出口 IP 白名单。",
        "retryable": False,
    }


def test_open_orders_uses_gateway_retry_and_structured_errors() -> None:
    fake = FakeClient()
    attempts = 0

    def transient(**_params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise Timeout("first attempt")
        return [{"orderId": 42}]

    fake.futures_get_open_orders = transient
    response = _api(fake).get("/api/orders/open?symbol=SOLUSDT")

    assert response.status_code == 200
    assert response.json() == [{"orderId": 42}]
    assert attempts == 2


def test_non_binance_orders_reject_before_gateway_or_service_calls(monkeypatch) -> None:
    fake = FakeClient()
    client = _api(fake)
    app_state.exchange_provider = "gateio"

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("Binance-backed service must not be called")

    monkeypatch.setattr(orders.account_analytics_service, "snapshot", unexpected_call)
    monkeypatch.setattr(orders.open_order_service, "combined", unexpected_call)

    for path in (
        "/api/orders/open",
        "/api/orders/open/combined",
        "/api/orders/analytics",
        "/api/orders/history",
        "/api/orders/trades",
    ):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"]["provider"] == "gateio"

    assert fake.__dict__.get("calls", []) == []
