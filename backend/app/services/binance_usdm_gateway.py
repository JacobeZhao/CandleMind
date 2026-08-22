"""Typed USD-M Futures reads with bounded retry and sanitized failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any

from .binance_errors import (
    BinanceFailure,
    BinanceFailureCategory,
    BinanceGatewayAuthenticationError,
    BinanceGatewayError,
    BinanceGatewayRejected,
    BinanceGatewayUnavailable,
    classify_binance_failure,
    invalid_response_failure,
)
from .binance_retry import BinanceOperation, BinanceRetryExecutor


@dataclass(frozen=True)
class ExchangeScope:
    account_fingerprint: str
    network: str
    symbol: str


class BinanceUsdMOperation(str, Enum):
    ACCOUNT = "account"
    ACCOUNT_BALANCE = "account_balance"
    ACCOUNT_TRADES = "account_trades"
    ALL_ORDERS = "all_orders"
    EXCHANGE_INFO = "exchange_info"
    FUNDING_RATE = "funding_rate"
    INCOME_HISTORY = "income_history"
    KLINES = "klines"
    MARK_PRICE = "mark_price"
    OPEN_ALGO_ORDERS = "open_algo_orders"
    OPEN_ORDERS = "open_orders"
    ORDER_LOOKUP = "order_lookup"
    PING = "ping"
    POSITION_INFORMATION = "position_information"
    POSITION_MODE = "position_mode"
    SERVER_TIME = "server_time"
    SYMBOL_CONFIG = "symbol_config"
    SYMBOL_TICKER = "symbol_ticker"
    TICKER = "ticker"


def exchange_scope(client: Any, symbol: str) -> ExchangeScope:
    api_key = getattr(client, "API_KEY", None)
    if not isinstance(api_key, str) or not api_key:
        raise BinanceGatewayRejected("active account identity is unavailable")
    normalized = symbol.strip().upper()
    if not normalized.isalnum():
        raise ValueError("invalid symbol")
    return ExchangeScope(
        account_fingerprint="sha256:" + sha256(api_key.encode("utf-8")).hexdigest(),
        network="testnet" if bool(getattr(client, "testnet", False)) else "mainnet",
        symbol=normalized,
    )


class BinanceUsdMGateway:
    """Small adapter around the public methods exposed by python-binance 1.0.37."""

    def __init__(
        self, client: Any, *, retry_executor: BinanceRetryExecutor | None = None
    ) -> None:
        self.client = client
        self.retry_executor = retry_executor or BinanceRetryExecutor()

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.OPEN_ORDERS,
            self.client.futures_get_open_orders,
            symbol=symbol,
        )

    def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        method = getattr(self.client, "futures_get_open_algo_orders", None)
        if method is None:
            raise BinanceGatewayRejected("installed Binance SDK lacks Algo order support")
        return self._call_list(BinanceUsdMOperation.OPEN_ALGO_ORDERS, method, symbol=symbol)

    def account_trades(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.ACCOUNT_TRADES,
            self.client.futures_account_trades,
            **params,
        )

    def all_orders(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.ALL_ORDERS,
            self.client.futures_get_all_orders,
            **params,
        )

    def account(self) -> dict[str, Any]:
        return self._call_dict(BinanceUsdMOperation.ACCOUNT, self.client.futures_account)

    def account_balance(self) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.ACCOUNT_BALANCE,
            self.client.futures_account_balance,
        )

    def position_information(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.POSITION_INFORMATION,
            self.client.futures_position_information,
            **params,
        )

    def symbol_config(self, **params: Any) -> list[dict[str, Any]]:
        method = getattr(self.client, "futures_symbol_config", None)
        if method is None:
            raise BinanceGatewayRejected("installed Binance SDK lacks symbol config support")
        return self._call_list(
            BinanceUsdMOperation.SYMBOL_CONFIG,
            method,
            **params,
        )

    def exchange_info(self) -> dict[str, Any]:
        return self._call_dict(
            BinanceUsdMOperation.EXCHANGE_INFO,
            self.client.futures_exchange_info,
        )

    def mark_price(self, **params: Any) -> dict[str, Any]:
        return self._call_dict(
            BinanceUsdMOperation.MARK_PRICE,
            self.client.futures_mark_price,
            **params,
        )

    def server_time(self) -> int:
        return self._call(
            BinanceUsdMOperation.SERVER_TIME,
            self.client.futures_time,
            _server_time_payload,
            BinanceOperation.READ,
        )

    def ping(self) -> dict[str, Any]:
        return self._call_dict(BinanceUsdMOperation.PING, self.client.futures_ping)

    def klines(self, **params: Any) -> list[list[Any]]:
        return self._call(
            BinanceUsdMOperation.KLINES,
            self.client.futures_klines,
            _klines_payload,
            BinanceOperation.READ,
            **params,
        )

    def symbol_ticker(self, **params: Any) -> dict[str, Any] | list[dict[str, Any]]:
        return self._call(
            BinanceUsdMOperation.SYMBOL_TICKER,
            self.client.futures_symbol_ticker,
            _dict_or_list_payload,
            BinanceOperation.READ,
            **params,
        )

    def funding_rate(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.FUNDING_RATE,
            self.client.futures_funding_rate,
            **params,
        )

    def position_mode(self) -> dict[str, Any]:
        return self._call_dict(
            BinanceUsdMOperation.POSITION_MODE,
            self.client.futures_get_position_mode,
        )

    def order(self, **params: Any) -> dict[str, Any]:
        return self._call_dict(
            BinanceUsdMOperation.ORDER_LOOKUP,
            self.client.futures_get_order,
            operation=BinanceOperation.WRITE_RECONCILE,
            **params,
        )

    def income_history(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.INCOME_HISTORY,
            self.client.futures_income_history,
            **params,
        )

    def ticker(self, **params: Any) -> dict[str, Any] | list[dict[str, Any]]:
        return self._call(
            BinanceUsdMOperation.TICKER,
            self.client.futures_ticker,
            _dict_or_list_payload,
            BinanceOperation.READ,
            **params,
        )

    def reconcile_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.OPEN_ORDERS,
            self.client.futures_get_open_orders,
            operation=BinanceOperation.WRITE_RECONCILE,
            symbol=symbol,
        )

    def reconcile_position_information(self, **params: Any) -> list[dict[str, Any]]:
        return self._call_list(
            BinanceUsdMOperation.POSITION_INFORMATION,
            self.client.futures_position_information,
            operation=BinanceOperation.WRITE_RECONCILE,
            **params,
        )

    def _call_list(
        self,
        label: BinanceUsdMOperation,
        method: Any,
        *,
        operation: BinanceOperation = BinanceOperation.READ,
        **params: Any,
    ) -> list[dict[str, Any]]:
        return self._call(label, method, _list_payload, operation, **params)

    def _call_dict(
        self,
        label: BinanceUsdMOperation,
        method: Any,
        *,
        operation: BinanceOperation = BinanceOperation.READ,
        **params: Any,
    ) -> dict[str, Any]:
        return self._call(label, method, _dict_payload, operation, **params)

    def _call(
        self,
        label: BinanceUsdMOperation,
        method: Any,
        validator: Any,
        operation: BinanceOperation,
        **params: Any,
    ):
        try:
            payload = self.retry_executor.run(operation, lambda: method(**params))
        except Exception as exc:
            failure = classify_binance_failure(exc)
            error = _gateway_error(failure)
            error.operation = label.value
            raise error from exc
        try:
            return validator(payload)
        except (TypeError, ValueError) as exc:
            failure = invalid_response_failure()
            error = BinanceGatewayRejected(failure.safe_message, failure=failure)
            error.operation = label.value
            raise error from exc


def _list_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise TypeError("expected a list of objects")
    return payload


def _dict_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("expected an object")
    return payload


def _dict_or_list_payload(payload: Any) -> dict[str, Any] | list[dict[str, Any]]:
    if isinstance(payload, dict):
        return payload
    return _list_payload(payload)


def _klines_payload(payload: Any) -> list[list[Any]]:
    if not isinstance(payload, list) or any(not isinstance(row, list) for row in payload):
        raise TypeError("expected a list of kline rows")
    return payload


def _server_time_payload(payload: Any) -> int:
    value = _dict_payload(payload).get("serverTime")
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected integer serverTime")
    return value


def gateway_error_detail(exc: BinanceGatewayError) -> dict[str, Any]:
    failure = exc.failure
    category = failure.category if failure else BinanceFailureCategory.REJECTED
    code, message = _API_ERROR_DETAILS[category]
    return {
        "code": code,
        "message": message,
        "retryable": bool(failure and failure.retryable),
    }


def gateway_error_status(exc: BinanceGatewayError) -> int:
    failure = exc.failure
    category = failure.category if failure else BinanceFailureCategory.REJECTED
    return _API_ERROR_STATUSES[category]


_API_ERROR_DETAILS = {
    BinanceFailureCategory.TRANSPORT: (
        "binance_unavailable",
        "Binance 暂时不可用，服务器已完成自动重试。",
    ),
    BinanceFailureCategory.TIMEOUT: (
        "binance_unavailable",
        "Binance 请求超时，服务器已完成自动重试。",
    ),
    BinanceFailureCategory.RATE_LIMITED: (
        "binance_rate_limited",
        "Binance 请求频率受限，请稍后重试。",
    ),
    BinanceFailureCategory.UPSTREAM: (
        "binance_unavailable",
        "Binance 暂时不可用，服务器已完成自动重试。",
    ),
    BinanceFailureCategory.TLS: (
        "binance_tls_error",
        "Binance 安全连接校验失败，请检查代理和证书配置。",
    ),
    BinanceFailureCategory.AUTHENTICATION: (
        "binance_access_rejected",
        "Binance 拒绝了账户请求，请核对 API Key、USD-M 合约权限和后端出口 IP 白名单。",
    ),
    BinanceFailureCategory.PERMISSION: (
        "binance_access_denied",
        "Binance 拒绝了当前操作，请检查 API 权限。",
    ),
    BinanceFailureCategory.INVALID_INPUT: (
        "binance_request_rejected",
        "Binance 拒绝了请求参数。",
    ),
    BinanceFailureCategory.GEO_RESTRICTED: (
        "binance_geo_restricted",
        "Binance 明确拒绝了当前出口地区的访问，请检查后端出口 IP。",
    ),
    BinanceFailureCategory.REJECTED: (
        "binance_request_rejected",
        "Binance 拒绝了请求。",
    ),
    BinanceFailureCategory.INVALID_RESPONSE: (
        "binance_response_invalid",
        "Binance 返回了无法识别的响应。",
    ),
    BinanceFailureCategory.WAF: (
        "binance_waf_rejected",
        "Binance 基础设施策略拒绝了请求，请降低请求频率并检查出口网络。",
    ),
}

_API_ERROR_STATUSES = {
    BinanceFailureCategory.TRANSPORT: 503,
    BinanceFailureCategory.TIMEOUT: 503,
    BinanceFailureCategory.RATE_LIMITED: 429,
    BinanceFailureCategory.UPSTREAM: 503,
    BinanceFailureCategory.TLS: 502,
    BinanceFailureCategory.AUTHENTICATION: 401,
    BinanceFailureCategory.PERMISSION: 403,
    BinanceFailureCategory.INVALID_INPUT: 502,
    BinanceFailureCategory.GEO_RESTRICTED: 451,
    BinanceFailureCategory.REJECTED: 502,
    BinanceFailureCategory.INVALID_RESPONSE: 502,
    BinanceFailureCategory.WAF: 403,
}


def _gateway_error(failure: BinanceFailure) -> BinanceGatewayError:
    if failure.category == BinanceFailureCategory.AUTHENTICATION:
        return BinanceGatewayAuthenticationError(failure.safe_message, failure=failure)
    if failure.retryable:
        return BinanceGatewayUnavailable(failure.safe_message, failure=failure)
    return BinanceGatewayRejected(failure.safe_message, failure=failure)
