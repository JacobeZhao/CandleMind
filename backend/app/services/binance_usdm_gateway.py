"""Read-only USD-M Futures access with sanitized failure categories."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from binance.exceptions import BinanceAPIException, BinanceRequestException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout


class BinanceGatewayError(RuntimeError):
    code = "upstream_error"


class BinanceGatewayUnavailable(BinanceGatewayError):
    code = "upstream_unavailable"


class BinanceGatewayRejected(BinanceGatewayError):
    code = "upstream_rejected"


class BinanceGatewayAuthenticationError(BinanceGatewayRejected):
    code = "authentication_failed"


@dataclass(frozen=True)
class ExchangeScope:
    account_fingerprint: str
    network: str
    symbol: str


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

    def __init__(self, client: Any) -> None:
        self.client = client

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return self._call(self.client.futures_get_open_orders, symbol=symbol)

    def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        method = getattr(self.client, "futures_get_open_algo_orders", None)
        if method is None:
            raise BinanceGatewayRejected("installed Binance SDK lacks Algo order support")
        return self._call(method, symbol=symbol)

    def account_trades(self, **params: Any) -> list[dict[str, Any]]:
        return self._call(self.client.futures_account_trades, **params)

    def all_orders(self, **params: Any) -> list[dict[str, Any]]:
        return self._call(self.client.futures_get_all_orders, **params)

    @staticmethod
    def _call(method, **params: Any) -> list[dict[str, Any]]:
        try:
            payload = method(**params)
        except (
            RequestsTimeout,
            RequestsConnectionError,
            TimeoutError,
            ConnectionError,
        ) as exc:
            raise BinanceGatewayUnavailable("Binance is temporarily unavailable") from exc
        except BinanceAPIException as exc:
            if getattr(exc, "code", None) == -2015:
                raise BinanceGatewayAuthenticationError(
                    "Binance API Key, futures permission, or IP allowlist rejected the request"
                ) from exc
            raise BinanceGatewayRejected("Binance rejected or returned an invalid response") from exc
        except BinanceRequestException as exc:
            raise BinanceGatewayRejected("Binance rejected or returned an invalid response") from exc
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise BinanceGatewayRejected("Binance returned an invalid response")
        return payload
