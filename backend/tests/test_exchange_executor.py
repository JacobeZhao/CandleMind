from decimal import Decimal

import pytest
from binance.exceptions import BinanceRequestException
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.services.exchange_executor import (
    ExchangeExecutionError,
    ExchangeExecutor,
    OrderIntent,
    OrderIntentType,
    RecoveryRequiredError,
    SymbolRules,
    UnsupportedAccountError,
    deterministic_client_order_id,
)


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "1000", "stepSize": "0.01"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "maxQty": "100", "stepSize": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    ]
}


class FakeClient:
    def __init__(self) -> None:
        self.created = []
        self.lookups = []
        self.dual_side = False
        self.positions = [{"symbol": "SOLUSDT", "positionAmt": "0"}]
        self.open_orders = []
        self.balances = [{"asset": "USDT", "availableBalance": "750.25"}]
        self.create_result = {
            "symbol": "SOLUSDT",
            "orderId": 123,
            "clientOrderId": "server-client-id",
            "status": "FILLED",
            "side": "BUY",
            "executedQty": "1.2",
            "avgPrice": "100.5",
        }
        self.create_error = None
        self.lookup_result = None
        self.lookup_error = None

    def futures_create_order(self, **params):
        self.created.append(params)
        if self.create_error:
            raise self.create_error
        return self.create_result

    def futures_get_order(self, **params):
        self.lookups.append(params)
        if self.lookup_error:
            raise self.lookup_error
        return self.lookup_result

    def futures_get_position_mode(self):
        return {"dualSidePosition": self.dual_side}

    def futures_position_information(self, **_params):
        return self.positions

    def futures_get_open_orders(self, **_params):
        return self.open_orders

    def futures_account_balance(self):
        return self.balances


@pytest.fixture
def rules():
    return SymbolRules.from_exchange_info(EXCHANGE_INFO, "solusdt")


def test_parses_market_filters_and_floors_layer_quantity(rules):
    executor = ExchangeExecutor(FakeClient(), rules)

    quantity = executor.layer_quantity(
        available_balance="1000",
        capital_limit="600",
        reference_price="99",
        layers=5,
        target_fraction="0.5",
    )

    assert rules.step_size == Decimal("0.1")
    assert quantity == Decimal("0.6")


def test_rejects_below_minimum_notional(rules):
    executor = ExchangeExecutor(FakeClient(), rules)

    with pytest.raises(ExchangeExecutionError, match="notional"):
        executor.layer_quantity(
            available_balance="4",
            capital_limit="4",
            reference_price="10",
            layers=1,
        )


def test_client_order_id_is_stable_and_within_binance_limit():
    intent = OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1"), "bar:123", 2)

    first = deterministic_client_order_id(intent)

    assert first == deterministic_client_order_id(intent)
    assert first != deterministic_client_order_id(
        OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1"), "bar:123", 3)
    )
    assert len(first) <= 36


@pytest.mark.parametrize(
    ("action", "direction", "side", "reduce_only"),
    [
        (OrderIntentType.OPEN, 1, "BUY", False),
        (OrderIntentType.ADD, -1, "SELL", False),
        (OrderIntentType.CLOSE, 1, "SELL", True),
        (OrderIntentType.CLOSE, -1, "BUY", True),
    ],
)
def test_maps_intents_to_one_way_market_orders(rules, action, direction, side, reduce_only):
    client = FakeClient()
    executor = ExchangeExecutor(client, rules)
    intent = OrderIntent("solusdt", action, direction, Decimal("1.27"), "decision-1")

    result = executor.execute(intent)

    params = client.created[0]
    assert params["symbol"] == "SOLUSDT"
    assert params["side"] == side
    assert params["type"] == "MARKET"
    assert params["quantity"] == "1.2"
    assert params["positionSide"] == "BOTH"
    assert params.get("reduceOnly", False) is reduce_only
    assert result.status == "FILLED"
    assert result.executed_quantity == Decimal("1.2")
    assert result.average_price == Decimal("100.5")


def test_ambiguous_submit_is_looked_up_without_resubmit(rules):
    client = FakeClient()
    client.create_error = BinanceRequestException("connection dropped")
    client.lookup_result = {
        "symbol": "SOLUSDT",
        "orderId": 456,
        "clientOrderId": "recovered",
        "status": "FILLED",
        "side": "BUY",
        "executedQty": "1.2",
        "avgPrice": "101",
    }
    executor = ExchangeExecutor(client, rules)
    intent = OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-2")

    result = executor.execute(intent)

    assert len(client.created) == 1
    assert client.lookups == [
        {"symbol": "SOLUSDT", "origClientOrderId": deterministic_client_order_id(intent)}
    ]
    assert result.recovered_after_ambiguous_submit is True
    assert result.order_id == 456


def test_non_terminal_market_response_is_queried_until_filled(rules, monkeypatch):
    client = FakeClient()
    client.create_result = {
        "symbol": "SOLUSDT", "orderId": 123, "status": "NEW", "side": "BUY",
        "executedQty": "0", "avgPrice": "0",
    }
    client.lookup_result = {
        "symbol": "SOLUSDT", "orderId": 123, "status": "FILLED", "side": "BUY",
        "executedQty": "1.2", "avgPrice": "100",
    }
    monkeypatch.setattr("backend.app.services.exchange_executor.time.sleep", lambda _value: None)

    result = ExchangeExecutor(client, rules).execute(
        OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-new")
    )

    assert result.status == "FILLED"
    assert len(client.created) == 1
    assert len(client.lookups) == 1


@pytest.mark.parametrize("transport_error", [TimeoutError("timed out"), RequestsTimeout("timed out")])
def test_unknown_ambiguous_submit_requires_recovery(rules, transport_error):
    client = FakeClient()
    client.create_error = transport_error
    client.lookup_error = RuntimeError("not found")
    executor = ExchangeExecutor(client, rules)

    with pytest.raises(RecoveryRequiredError, match="unknown exchange state"):
        executor.execute(
            OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-3")
        )
    assert len(client.created) == 1


def test_account_validation_rejects_hedge_mode(rules):
    client = FakeClient()
    client.dual_side = True

    with pytest.raises(UnsupportedAccountError, match="hedge mode"):
        ExchangeExecutor(client, rules).validate_one_way_account("SOLUSDT")


def test_account_validation_rejects_unreconciled_position_and_orders(rules):
    client = FakeClient()
    executor = ExchangeExecutor(client, rules)
    client.positions = [{"symbol": "SOLUSDT", "positionAmt": "2.5"}]

    with pytest.raises(UnsupportedAccountError, match="existing position"):
        executor.validate_one_way_account("SOLUSDT")

    client.positions = [{"symbol": "SOLUSDT", "positionAmt": "0"}]
    client.open_orders = [{"orderId": 1}]
    with pytest.raises(UnsupportedAccountError, match="open orders"):
        executor.validate_one_way_account("SOLUSDT")


def test_account_validation_can_return_reconciled_state(rules):
    client = FakeClient()
    client.positions = [{"symbol": "SOLUSDT", "positionAmt": "-2.5"}]
    client.open_orders = [{"orderId": 1}]

    result = ExchangeExecutor(client, rules).validate_one_way_account(
        "SOLUSDT", allow_existing_position=True, allow_open_orders=True
    )

    assert result.position_quantity == Decimal("-2.5")
    assert result.open_order_count == 1


def test_reads_available_balance_and_position_risk(rules):
    client = FakeClient()
    client.positions = [{
        "symbol": "SOLUSDT",
        "positionAmt": "-2.5",
        "entryPrice": "100.2",
        "isolated": True,
        "leverage": "1",
    }]
    executor = ExchangeExecutor(client, rules)

    assert executor.available_balance() == Decimal("750.25")
    position = executor.validate_symbol_risk("SOLUSDT")
    assert position.direction == -1
    assert position.quantity == Decimal("2.5")
    assert position.entry_price == Decimal("100.2")


def test_rejects_cross_margin_or_non_one_x_leverage(rules):
    client = FakeClient()
    client.positions = [{
        "symbol": "SOLUSDT",
        "positionAmt": "0",
        "entryPrice": "0",
        "isolated": False,
        "leverage": "20",
    }]
    executor = ExchangeExecutor(client, rules)

    with pytest.raises(UnsupportedAccountError, match="isolated"):
        executor.validate_symbol_risk("SOLUSDT")
