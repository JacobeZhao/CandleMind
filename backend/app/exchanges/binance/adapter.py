"""Adapters around the existing Binance gateway and execution service."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from backend.app.services.binance_usdm_gateway import BinanceUsdMGateway
from backend.app.services.exchange_executor import (
    ExchangeExecutor,
    OrderIntent,
    OrderIntentType,
)

from ..contracts import (
    AccountBalance,
    ExchangeBinding,
    ExchangeCapabilities,
    ExchangeOrder,
    ExchangePosition,
    ExecutionAuthorization,
    Kline,
    MarketTicker,
    OrderAction,
    OrderRequest,
)
from ..registry import ExchangeAdapter


class BinanceAdapterError(RuntimeError):
    """Binance returned data that cannot satisfy the normalized contract."""


class BinanceMarketDataAdapter:
    __slots__ = ("_binding", "_gateway")

    def __init__(self, binding: ExchangeBinding, gateway: BinanceUsdMGateway) -> None:
        _require_binance(binding)
        self._binding = binding
        self._gateway = gateway

    @property
    def binding(self) -> ExchangeBinding:
        return self._binding

    def server_time(self) -> int:
        server_time = _int(self._gateway.server_time(), "server time")
        if server_time <= 0:
            raise BinanceAdapterError("invalid server time")
        return server_time

    def ticker(self) -> MarketTicker:
        payload = self._gateway.ticker(symbol=self.binding.symbol)
        if not isinstance(payload, dict):
            raise BinanceAdapterError("expected one ticker for the bound symbol")
        return MarketTicker(
            symbol=_matching_symbol(payload.get("symbol"), self.binding.symbol),
            last_price=_decimal(payload.get("lastPrice"), "last price"),
            high_24h=_decimal(payload.get("highPrice"), "24h high"),
            low_24h=_decimal(payload.get("lowPrice"), "24h low"),
            close_time_ms=_optional_int(payload.get("closeTime"), "close time"),
        )

    def completed_klines(self, interval: str, limit: int) -> tuple[Kline, ...]:
        normalized_interval = interval.strip()
        if not normalized_interval or not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("interval and limit are invalid")
        if limit < 1 or limit > 1500:
            raise ValueError("limit must be between 1 and 1500")
        server_time = self.server_time()
        rows = self._gateway.klines(
            symbol=self.binding.symbol,
            interval=normalized_interval,
            limit=limit,
        )
        completed: list[Kline] = []
        for row in rows:
            if len(row) < 7:
                raise BinanceAdapterError("incomplete Binance kline")
            close_time = _int(row[6], "close time")
            if close_time >= server_time:
                continue
            completed.append(
                Kline(
                    symbol=self.binding.symbol,
                    interval=normalized_interval,
                    open_time_ms=_int(row[0], "open time"),
                    close_time_ms=close_time,
                    open=_decimal(row[1], "open"),
                    high=_decimal(row[2], "high"),
                    low=_decimal(row[3], "low"),
                    close=_decimal(row[4], "close"),
                    volume=_decimal(row[5], "volume"),
                    closed=True,
                )
            )
        return tuple(completed)


class BinanceAccountAdapter:
    __slots__ = ("_binding", "_executor")

    def __init__(self, binding: ExchangeBinding, executor: ExchangeExecutor) -> None:
        _require_binance(binding)
        self._binding = binding
        self._executor = executor

    @property
    def binding(self) -> ExchangeBinding:
        return self._binding

    def available_balance(self, asset: str = "USDT") -> AccountBalance:
        if not isinstance(asset, str):
            raise ValueError("invalid balance asset")
        normalized = asset.strip().upper()
        if not normalized or not normalized.isalnum():
            raise ValueError("invalid balance asset")
        available = _decimal(self._executor.available_balance(normalized), "available balance")
        return AccountBalance(normalized, available)

    def current_position(self) -> ExchangePosition:
        position = self._executor.current_position(self.binding.symbol)
        symbol = _matching_symbol(position.symbol, self.binding.symbol)
        direction = _int(position.direction, "position direction")
        if direction not in (-1, 0, 1):
            raise BinanceAdapterError("invalid position direction")
        leverage = _int(position.leverage, "position leverage")
        if leverage < 1:
            raise BinanceAdapterError("invalid position leverage")
        return ExchangePosition(
            symbol=symbol,
            direction=direction,
            quantity=_decimal(position.quantity, "position quantity"),
            entry_price=_decimal(position.entry_price, "position entry price"),
            isolated=_boolean(position.isolated, "position isolated flag"),
            leverage=leverage,
        )


class BinanceTradingAdapter:
    __slots__ = ("_binding", "_executor")

    def __init__(self, binding: ExchangeBinding, executor: ExchangeExecutor) -> None:
        _require_binance(binding)
        self._binding = binding
        self._executor = executor

    @property
    def binding(self) -> ExchangeBinding:
        return self._binding

    def execute(
        self, request: OrderRequest, authorization: ExecutionAuthorization
    ) -> ExchangeOrder:
        authorization.require(self.binding)
        self._require_request(request)
        return _order(self._executor.execute(_intent(request)), self.binding.symbol)

    def lookup(
        self, request: OrderRequest, authorization: ExecutionAuthorization
    ) -> ExchangeOrder:
        authorization.require(self.binding)
        self._require_request(request)
        return _order(self._executor.lookup(_intent(request)), self.binding.symbol)

    def _require_request(self, request: OrderRequest) -> None:
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be an OrderRequest")
        if request.symbol != self.binding.symbol:
            raise ValueError("order symbol does not match the adapter binding")


def build_binance_adapter(
    binding: ExchangeBinding,
    gateway: BinanceUsdMGateway,
    executor: ExchangeExecutor,
) -> ExchangeAdapter:
    """Compose ports around existing services without exposing their SDK client."""

    market = BinanceMarketDataAdapter(binding, gateway)
    account = BinanceAccountAdapter(binding, executor)
    trading = BinanceTradingAdapter(binding, executor)
    return ExchangeAdapter(
        binding=binding,
        capabilities=ExchangeCapabilities(market_data=True, account=True, trading=True),
        market=market,
        account=account,
        trading=trading,
    )


def _require_binance(binding: ExchangeBinding) -> None:
    if binding.provider != "binance":
        raise ValueError("Binance adapter requires provider 'binance'")


def _intent(request: OrderRequest) -> OrderIntent:
    return OrderIntent(
        symbol=request.symbol,
        action=OrderIntentType(request.action.value),
        direction=request.direction,
        quantity=request.quantity,
        decision_id=request.decision_id,
        ordinal=request.ordinal,
    )


def _order(result: Any, expected_symbol: str) -> ExchangeOrder:
    try:
        action = OrderAction(result.action.value)
    except (AttributeError, ValueError) as exc:
        raise BinanceAdapterError("invalid order action") from exc
    status = _text(result.status, "order status").upper()
    side = _text(result.side, "order side").upper()
    client_order_id = _text(result.client_order_id, "client order ID")
    if side not in {"BUY", "SELL"}:
        raise BinanceAdapterError("invalid normalized order result")
    return ExchangeOrder(
        symbol=_matching_symbol(result.symbol, expected_symbol),
        action=action,
        status=status,
        side=side,
        requested_quantity=_decimal(result.quantity, "requested quantity"),
        executed_quantity=_decimal(result.executed_quantity, "executed quantity"),
        average_price=_decimal(result.average_price, "average price"),
        order_id=_optional_identifier(result.order_id, "order ID"),
        client_order_id=client_order_id,
        reconciled=_boolean(
            result.recovered_after_ambiguous_submit,
            "order reconciliation flag",
        ),
    )


def _matching_symbol(value: Any, expected: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized != expected:
        raise BinanceAdapterError("ticker symbol does not match the adapter binding")
    return normalized


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BinanceAdapterError(f"invalid {label}") from exc
    if not result.is_finite() or result < 0:
        raise BinanceAdapterError(f"invalid {label}")
    return result


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise BinanceAdapterError(f"invalid {label}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BinanceAdapterError(f"invalid {label}") from exc


def _optional_int(value: Any, label: str) -> int | None:
    return None if value is None else _int(value, label)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BinanceAdapterError(f"invalid {label}")
    return value.strip()


def _optional_identifier(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BinanceAdapterError(f"invalid {label}")
    normalized = str(value).strip()
    if not normalized:
        raise BinanceAdapterError(f"invalid {label}")
    return normalized


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BinanceAdapterError(f"invalid {label}")
    return value
