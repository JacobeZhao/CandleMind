"""Exchange-neutral capability contracts and normalized data objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import os
from typing import Protocol, runtime_checkable


MAINNET_ENABLE_ENV = "CANDLEMIND_MAINNET_TRADING_ENABLED"


class ExchangeNetwork(str, Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class OrderAction(str, Enum):
    OPEN = "open"
    ADD = "add"
    CLOSE = "close"


def normalize_provider(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or not normalized.replace("_", "").isalnum():
        raise ValueError("invalid exchange provider")
    return normalized


def normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized or not normalized.isalnum():
        raise ValueError("invalid exchange symbol")
    return normalized


@dataclass(frozen=True, slots=True)
class ExchangeBinding:
    provider: str
    network: ExchangeNetwork
    symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_provider(self.provider))
        object.__setattr__(self, "network", ExchangeNetwork(self.network))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    market_data: bool = True
    account: bool = False
    trading: bool = False


@dataclass(frozen=True, slots=True)
class MarketTicker:
    symbol: str
    last_price: Decimal
    high_24h: Decimal
    low_24h: Decimal
    close_time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool


@dataclass(frozen=True, slots=True)
class AccountBalance:
    asset: str
    available: Decimal


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    symbol: str
    direction: int
    quantity: Decimal
    entry_price: Decimal
    isolated: bool
    leverage: int


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    action: OrderAction
    direction: int
    quantity: Decimal
    decision_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "action", OrderAction(self.action))
        if self.direction not in (-1, 1):
            raise ValueError("order direction must be -1 or 1")
        quantity = _positive_decimal(self.quantity, "order quantity")
        object.__setattr__(self, "quantity", quantity)
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise ValueError("decision ID must be non-empty")
        object.__setattr__(self, "decision_id", self.decision_id.strip())
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("order ordinal must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    symbol: str
    action: OrderAction
    status: str
    side: str
    requested_quantity: Decimal
    executed_quantity: Decimal
    average_price: Decimal
    order_id: str | None
    client_order_id: str
    reconciled: bool


class ExecutionAuthorizationError(PermissionError):
    """Execution was requested outside its server-authorized scope."""


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """A capability bound to one provider, network, and symbol."""

    binding: ExchangeBinding

    @classmethod
    def issue(cls, binding: ExchangeBinding) -> "ExecutionAuthorization":
        authorization = cls(binding)
        authorization.require(binding)
        return authorization

    def require(self, binding: ExchangeBinding) -> None:
        if self.binding != binding:
            raise ExecutionAuthorizationError(
                "execution authorization does not match provider, network, and symbol"
            )
        if binding.network is ExchangeNetwork.MAINNET and not _mainnet_enabled():
            raise ExecutionAuthorizationError(
                "mainnet execution is disabled by the server"
            )


def _mainnet_enabled() -> bool:
    return os.environ.get(MAINNET_ENABLE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return result


@runtime_checkable
class MarketDataPort(Protocol):
    @property
    def binding(self) -> ExchangeBinding: ...

    def server_time(self) -> int: ...

    def ticker(self) -> MarketTicker: ...

    def completed_klines(self, interval: str, limit: int) -> tuple[Kline, ...]: ...


@runtime_checkable
class AccountPort(Protocol):
    @property
    def binding(self) -> ExchangeBinding: ...

    def available_balance(self, asset: str = "USDT") -> AccountBalance: ...

    def current_position(self) -> ExchangePosition: ...


@runtime_checkable
class TradingPort(Protocol):
    @property
    def binding(self) -> ExchangeBinding: ...

    def execute(
        self,
        request: OrderRequest,
        authorization: ExecutionAuthorization,
    ) -> ExchangeOrder: ...

    def lookup(
        self,
        request: OrderRequest,
        authorization: ExecutionAuthorization,
    ) -> ExchangeOrder: ...
