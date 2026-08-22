"""Conservative Binance USD-M Futures order execution primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
import hashlib
import os
import time
from typing import Any, Protocol

from binance.exceptions import BinanceRequestException

from .binance_errors import (
    BinanceFailureCategory,
    BinanceGatewayAuthenticationError,
    BinanceGatewayRejected,
    BinanceGatewayUnavailable,
    BinanceSubmissionOutcome,
    classify_binance_failure,
)
from .binance_retry import BinanceOperation
from .binance_usdm_gateway import BinanceUsdMGateway


class ExchangeExecutionError(RuntimeError):
    """Base class for execution validation and exchange failures."""


class RecoveryRequiredError(ExchangeExecutionError):
    """The exchange may have accepted an order, but its result is unknown."""


class UnsupportedAccountError(ExchangeExecutionError):
    """The account has state that this executor cannot safely manage."""


class OrderIntentType(str, Enum):
    OPEN = "open"
    ADD = "add"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    action: OrderIntentType
    direction: int
    quantity: Decimal
    decision_id: str
    ordinal: int = 0


@dataclass(frozen=True, slots=True)
class SymbolRules:
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, exchange_info: dict[str, Any], symbol: str) -> "SymbolRules":
        normalized = _symbol(symbol)
        symbol_info = next(
            (item for item in exchange_info.get("symbols", []) if item.get("symbol") == normalized),
            None,
        )
        if symbol_info is None:
            raise ExchangeExecutionError(f"exchange rules not found for {normalized}")

        filters = {item.get("filterType"): item for item in symbol_info.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
        if not lot or not notional:
            raise ExchangeExecutionError(f"incomplete exchange rules for {normalized}")
        rules = cls(
            step_size=_positive_decimal(lot.get("stepSize"), "step size"),
            min_quantity=_positive_decimal(lot.get("minQty"), "minimum quantity"),
            max_quantity=_positive_decimal(lot.get("maxQty"), "maximum quantity"),
            min_notional=_positive_decimal(
                notional.get("notional", notional.get("minNotional")),
                "minimum notional",
            ),
        )
        if rules.min_quantity > rules.max_quantity:
            raise ExchangeExecutionError("minimum quantity exceeds maximum quantity")
        return rules

    def floor_quantity(self, quantity: Decimal | str | int | float) -> Decimal:
        value = _positive_decimal(quantity, "quantity")
        floored = (value / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size
        if floored < self.min_quantity:
            raise ExchangeExecutionError("quantity is below the exchange minimum")
        if floored > self.max_quantity:
            raise ExchangeExecutionError("quantity exceeds the exchange maximum")
        return floored

    def validate_notional(self, quantity: Decimal, price: Decimal | str | int | float) -> None:
        reference_price = _positive_decimal(price, "reference price")
        if quantity * reference_price < self.min_notional:
            raise ExchangeExecutionError("order notional is below the exchange minimum")


@dataclass(frozen=True, slots=True)
class AccountValidation:
    symbol: str
    position_quantity: Decimal
    open_order_count: int


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    symbol: str
    direction: int
    quantity: Decimal
    entry_price: Decimal
    isolated: bool
    leverage: int


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    symbol: str
    action: OrderIntentType
    status: str
    side: str
    quantity: Decimal
    executed_quantity: Decimal
    average_price: Decimal
    order_id: int | None
    client_order_id: str
    recovered_after_ambiguous_submit: bool
    raw: dict[str, Any]


class FuturesClient(Protocol):
    def futures_create_order(self, **params: Any) -> dict[str, Any]: ...


class ExchangeExecutor:
    """Submit idempotent market orders for a validated one-way USD-M account."""

    def __init__(
        self,
        client: FuturesClient,
        rules: SymbolRules,
        network: str | None = None,
    ) -> None:
        if network is None:
            client_network = getattr(client, "testnet", None)
            if client_network is True:
                network = "testnet"
            elif client_network is False:
                network = "mainnet"
        if network not in {None, "testnet", "mainnet"}:
            raise ValueError("invalid execution network")
        self.client = client
        self.gateway = BinanceUsdMGateway(client)
        self.rules = rules
        self.network = network

    def layer_quantity(
        self,
        *,
        available_balance: Decimal | str | int | float,
        capital_limit: Decimal | str | int | float,
        reference_price: Decimal | str | int | float,
        layers: int,
        target_fraction: Decimal | str | int | float = Decimal("1"),
    ) -> Decimal:
        if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
            raise ExchangeExecutionError("layers must be a positive integer")
        available = _non_negative_decimal(available_balance, "available balance")
        limit = _positive_decimal(capital_limit, "capital limit")
        price = _positive_decimal(reference_price, "reference price")
        fraction = _positive_decimal(target_fraction, "target fraction")
        if fraction > 1:
            raise ExchangeExecutionError("target fraction must not exceed one")
        budget = min(available, limit) * fraction / layers
        quantity = self.rules.floor_quantity(budget / price)
        self.rules.validate_notional(quantity, price)
        return quantity

    def weighted_layer_quantity(
        self,
        *,
        available_balance: Decimal | str | int | float,
        capital_limit: Decimal | str | int | float,
        reference_price: Decimal | str | int | float,
        capital_weight: Decimal | str | int | float,
    ) -> Decimal:
        """Size one normalized layer without exceeding its capital allocation."""

        available = _non_negative_decimal(available_balance, "available balance")
        limit = _positive_decimal(capital_limit, "capital limit")
        price = _positive_decimal(reference_price, "reference price")
        weight = _positive_decimal(capital_weight, "capital weight")
        if weight > 1:
            raise ExchangeExecutionError("capital weight must not exceed one")
        budget = min(available, limit * weight)
        quantity = self.rules.floor_quantity(budget / price)
        self.rules.validate_notional(quantity, price)
        return quantity

    def validate_one_way_account(
        self,
        symbol: str,
        *,
        allow_existing_position: bool = False,
        allow_open_orders: bool = False,
    ) -> AccountValidation:
        normalized = _symbol(symbol)
        mode = self.gateway.position_mode()
        if bool(mode.get("dualSidePosition")):
            raise UnsupportedAccountError("hedge mode is not supported")

        positions = self.gateway.position_information(symbol=normalized)
        quantity = sum((_decimal(item.get("positionAmt", "0"), "position amount") for item in positions), Decimal("0"))
        if quantity and not allow_existing_position:
            raise UnsupportedAccountError("an existing position requires reconciliation")
        open_orders = self.gateway.reconcile_open_orders(normalized)
        algo_orders = self.gateway.open_algo_orders(normalized)
        open_order_count = len(open_orders) + len(algo_orders)
        if open_order_count and not allow_open_orders:
            raise UnsupportedAccountError("existing open orders require reconciliation")
        return AccountValidation(normalized, quantity, open_order_count)

    def available_balance(self, asset: str = "USDT") -> Decimal:
        balances = self.gateway.account_balance()
        row = next((item for item in balances if item.get("asset") == asset), None)
        if row is None:
            raise ExchangeExecutionError(f"{asset} futures balance is unavailable")
        return _non_negative_decimal(
            row.get("availableBalance", row.get("withdrawAvailable", "0")),
            "available balance",
        )

    def current_position(self, symbol: str) -> ExchangePosition:
        normalized = _symbol(symbol)
        rows = self.gateway.reconcile_position_information(symbol=normalized)
        if not rows:
            isolated, leverage = self._symbol_risk_config(normalized)
            return ExchangePosition(
                symbol=normalized,
                direction=0,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                isolated=isolated,
                leverage=leverage,
            )
        non_flat = [row for row in rows if _decimal(row.get("positionAmt", "0"), "position amount")]
        if len(non_flat) > 1:
            raise UnsupportedAccountError("multiple position rows require reconciliation")
        row = non_flat[0] if non_flat else rows[0]
        quantity = _decimal(row.get("positionAmt", "0"), "position amount")
        isolated = row.get("isolated")
        leverage = row.get("leverage")
        if isolated is None or leverage is None:
            config_isolated, config_leverage = self._symbol_risk_config(normalized)
            isolated = config_isolated if isolated is None else isolated
            leverage = config_leverage if leverage is None else leverage
        return ExchangePosition(
            symbol=normalized,
            direction=1 if quantity > 0 else -1 if quantity < 0 else 0,
            quantity=abs(quantity),
            entry_price=_non_negative_decimal(row.get("entryPrice", "0"), "entry price"),
            isolated=bool(isolated),
            leverage=int(leverage),
        )

    def _symbol_risk_config(self, symbol: str) -> tuple[bool, int]:
        configs = self.gateway.symbol_config(symbol=symbol)
        row = next((item for item in configs if item.get("symbol") == symbol), None)
        if row is None:
            raise ExchangeExecutionError(
                f"position information is unavailable for {symbol}"
            )
        return (
            str(row.get("marginType", "")).upper() == "ISOLATED",
            int(row.get("leverage", 0)),
        )

    def validate_symbol_risk(self, symbol: str) -> ExchangePosition:
        position = self.current_position(symbol)
        if not position.isolated:
            raise UnsupportedAccountError("the strategy requires isolated margin")
        if position.leverage != 1:
            raise UnsupportedAccountError("the strategy requires 1x leverage")
        return position

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        self._require_write_authorized()
        symbol = _symbol(intent.symbol)
        if intent.direction not in (-1, 1):
            raise ExchangeExecutionError("direction must be -1 or 1")
        if not intent.decision_id.strip() or intent.ordinal < 0:
            raise ExchangeExecutionError("decision ID and ordinal are invalid")
        quantity = self.rules.floor_quantity(intent.quantity)
        closing = intent.action is OrderIntentType.CLOSE
        side = "SELL" if (intent.direction == 1) == closing else "BUY"
        client_order_id = deterministic_client_order_id(intent)
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": _format_decimal(quantity),
            "positionSide": "BOTH",
            "newClientOrderId": client_order_id,
        }
        if closing:
            params["reduceOnly"] = True

        recovered = False
        try:
            payload = self.gateway.retry_executor.run(
                BinanceOperation.WRITE_SUBMIT,
                lambda: self.client.futures_create_order(**params),
            )
        except Exception as exc:
            failure = classify_binance_failure(exc)
            if failure.submission_outcome is BinanceSubmissionOutcome.NOT_ACCEPTED:
                error_type = (
                    BinanceGatewayUnavailable
                    if failure.retryable
                    else BinanceGatewayRejected
                )
                raise error_type(failure.safe_message, failure=failure) from exc
            if not (
                failure.retryable
                or isinstance(exc, (BinanceRequestException, OSError))
            ):
                error_type = (
                    BinanceGatewayAuthenticationError
                    if failure.category == BinanceFailureCategory.AUTHENTICATION
                    else BinanceGatewayRejected
                )
                raise error_type(failure.safe_message, failure=failure) from exc
            recovered = True
            try:
                payload = self.gateway.order(
                    symbol=symbol,
                    origClientOrderId=client_order_id,
                )
            except Exception as lookup_exc:
                raise RecoveryRequiredError(
                    f"order {client_order_id} has an unknown exchange state"
                ) from lookup_exc
            if not payload or not payload.get("status"):
                raise RecoveryRequiredError(
                    f"order {client_order_id} has an unknown exchange state"
                ) from exc
        result = _normalize_result(intent.action, side, quantity, client_order_id, payload, recovered)
        for _ in range(5):
            if result.status in {"FILLED", "REJECTED", "EXPIRED", "CANCELED"}:
                return result
            time.sleep(0.2)
            try:
                payload = self.gateway.order(
                    symbol=symbol,
                    origClientOrderId=client_order_id,
                )
            except Exception as exc:
                raise RecoveryRequiredError(
                    f"order {client_order_id} did not reach a known terminal state"
                ) from exc
            result = _normalize_result(
                intent.action, side, quantity, client_order_id, payload, recovered
            )
        raise RecoveryRequiredError(
            f"order {client_order_id} did not reach a terminal state"
        )

    def _require_write_authorized(self) -> None:
        if self.network not in {"testnet", "mainnet"}:
            raise UnsupportedAccountError(
                "execution network must be explicitly bound before writing"
            )
        if self.network != "mainnet":
            return
        enabled = os.environ.get(
            "CANDLEMIND_MAINNET_TRADING_ENABLED", ""
        ).strip().lower()
        if enabled not in {"1", "true", "yes"}:
            raise UnsupportedAccountError("mainnet execution is disabled by the server")

    def lookup(self, intent: OrderIntent) -> ExecutionResult:
        """Resolve a previously journaled intent without creating an order."""

        symbol = _symbol(intent.symbol)
        quantity = self.rules.floor_quantity(intent.quantity)
        closing = intent.action is OrderIntentType.CLOSE
        side = "SELL" if (intent.direction == 1) == closing else "BUY"
        client_order_id = deterministic_client_order_id(intent)
        try:
            payload = self.gateway.order(
                symbol=symbol,
                origClientOrderId=client_order_id,
            )
        except Exception as exc:
            raise RecoveryRequiredError(
                f"order {client_order_id} could not be reconciled"
            ) from exc
        if not payload or not payload.get("status"):
            raise RecoveryRequiredError(
                f"order {client_order_id} has an unknown exchange state"
            )
        return _normalize_result(
            intent.action,
            side,
            quantity,
            client_order_id,
            payload,
            True,
        )


def deterministic_client_order_id(intent: OrderIntent) -> str:
    """Build a stable, Binance-compatible identifier without exposing decision data."""
    symbol = _symbol(intent.symbol)
    source = f"{symbol}|{intent.action.value}|{intent.direction}|{intent.decision_id}|{intent.ordinal}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:30]
    return f"cm{digest}"


def _normalize_result(
    action: OrderIntentType,
    side: str,
    requested_quantity: Decimal,
    client_order_id: str,
    payload: dict[str, Any],
    recovered: bool,
) -> ExecutionResult:
    symbol = _symbol(str(payload.get("symbol", "")))
    executed = _non_negative_decimal(payload.get("executedQty", "0"), "executed quantity")
    average_price = _non_negative_decimal(payload.get("avgPrice", "0"), "average price")
    order_id_value = payload.get("orderId")
    return ExecutionResult(
        symbol=symbol,
        action=action,
        status=str(payload.get("status", "UNKNOWN")).upper(),
        side=str(payload.get("side", side)).upper(),
        quantity=requested_quantity,
        executed_quantity=executed,
        average_price=average_price,
        order_id=None if order_id_value is None else int(order_id_value),
        client_order_id=str(payload.get("clientOrderId", client_order_id)),
        recovered_after_ambiguous_submit=recovered,
        raw=dict(payload),
    )


def _symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized or not normalized.isalnum():
        raise ExchangeExecutionError("symbol must be non-empty and alphanumeric")
    return normalized


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeExecutionError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ExchangeExecutionError(f"{name} must be finite")
    return result


def _positive_decimal(value: Any, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ExchangeExecutionError(f"{name} must be positive")
    return result


def _non_negative_decimal(value: Any, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < 0:
        raise ExchangeExecutionError(f"{name} must not be negative")
    return result


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")
