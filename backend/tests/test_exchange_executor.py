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
from backend.app.services.binance_errors import BinanceGatewayUnavailable
from backend.app.services.binance_retry import (
    BinanceRetryExecutor,
    ProcessCooldown,
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
    testnet = True
    def __init__(self) -> None:
        self.created = []
        self.lookups = []
        self.dual_side = False
        self.positions = [{"symbol": "SOLUSDT", "positionAmt": "0"}]
        self.symbol_configs = [{
            "symbol": "SOLUSDT",
            "marginType": "ISOLATED",
            "leverage": 1,
        }]
        self.open_orders = []
        self.open_algo_orders = []
        self.open_orders_error = None
        self.open_algo_orders_error = None
        self.read_calls = []
        self.write_calls = []
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
        self.read_calls.append("position_mode")
        return {"dualSidePosition": self.dual_side}

    def futures_position_information(self, **_params):
        self.read_calls.append("positions")
        return self.positions

    def futures_symbol_config(self, **_params):
        self.read_calls.append("symbol_config")
        return self.symbol_configs

    def futures_get_open_orders(self, **_params):
        self.read_calls.append("open_orders")
        if self.open_orders_error:
            raise self.open_orders_error
        return self.open_orders

    def futures_get_open_algo_orders(self, **_params):
        self.read_calls.append("open_algo_orders")
        if self.open_algo_orders_error:
            raise self.open_algo_orders_error
        return self.open_algo_orders

    def futures_account_balance(self):
        return self.balances

    def futures_cancel_order(self, **params):
        self.write_calls.append(("cancel", params))

    def futures_change_leverage(self, **params):
        self.write_calls.append(("leverage", params))

    def futures_change_margin_type(self, **params):
        self.write_calls.append(("margin", params))


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


def test_weighted_layer_quantity_caps_each_layer_to_normalized_budget(rules):
    executor = ExchangeExecutor(FakeClient(), rules)

    quantity = executor.weighted_layer_quantity(
        available_balance="1000",
        capital_limit="600",
        reference_price="100",
        capital_weight="0.25",
    )

    assert quantity == Decimal("1.5")


def test_weighted_layer_quantity_rejects_weight_above_total_budget(rules):
    executor = ExchangeExecutor(FakeClient(), rules)

    with pytest.raises(ExchangeExecutionError, match="weight"):
        executor.weighted_layer_quantity(
            available_balance="1000",
            capital_limit="600",
            reference_price="100",
            capital_weight="1.01",
        )


def test_client_order_id_is_stable_and_within_binance_limit():
    intent = OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1"), "bar:123", 2)

    first = deterministic_client_order_id(intent)

    assert first == deterministic_client_order_id(intent)
    assert first != deterministic_client_order_id(
        OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1"), "bar:123", 3)
    )
    assert len(first) <= 36


def test_mainnet_write_is_denied_inside_executor_before_sdk_call(
    rules, monkeypatch
):
    client = FakeClient()
    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "false")
    executor = ExchangeExecutor(client, rules, "mainnet")

    with pytest.raises(UnsupportedAccountError, match="disabled"):
        executor.execute(
            OrderIntent(
                "SOLUSDT",
                OrderIntentType.OPEN,
                1,
                Decimal("1.2"),
                "mainnet-denied",
            )
        )

    assert client.created == []


def test_unbound_execution_network_is_denied_before_sdk_call(rules):
    class UnboundClient(FakeClient):
        testnet = None

    client = UnboundClient()
    executor = ExchangeExecutor(client, rules)

    with pytest.raises(UnsupportedAccountError, match="explicitly bound"):
        executor.execute(
            OrderIntent(
                "SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "unbound"
            )
        )

    assert client.created == []


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


def test_known_not_accepted_submit_retries_with_the_same_client_id(rules):
    client = FakeClient()
    attempts = []

    def create(**params):
        attempts.append(dict(params))
        if len(attempts) == 1:
            error = RuntimeError("private upstream response")
            error.status_code = 503
            error.code = -1008
            error.message = "Request throttled by system-level protection."
            error.response = None
            raise error
        return client.create_result

    client.futures_create_order = create
    executor = ExchangeExecutor(client, rules)
    executor.gateway.retry_executor = BinanceRetryExecutor(
        sleeper=lambda _delay: None,
        rng=lambda: 0,
        cooldown=ProcessCooldown(),
    )

    result = executor.execute(
        OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-safe-retry")
    )

    assert result.status == "FILLED"
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert client.lookups == []


def test_unknown_503_submit_is_only_reconciled(rules):
    client = FakeClient()
    error = RuntimeError("private upstream response")
    error.status_code = 503
    error.code = -1000
    error.message = "Unknown error, please check your request or try again later."
    error.response = None
    client.create_error = error
    client.lookup_result = {
        "symbol": "SOLUSDT",
        "orderId": 789,
        "clientOrderId": "recovered",
        "status": "FILLED",
        "side": "BUY",
        "executedQty": "1.2",
        "avgPrice": "101",
    }

    result = ExchangeExecutor(client, rules).execute(
        OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-unknown")
    )

    assert result.recovered_after_ambiguous_submit is True
    assert len(client.created) == 1
    assert len(client.lookups) == 1


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
    client.lookup_error = RequestsTimeout("lookup timed out")
    executor = ExchangeExecutor(client, rules)
    executor.gateway.retry_executor = BinanceRetryExecutor(
        sleeper=lambda _delay: None,
        rng=lambda: 0,
        cooldown=ProcessCooldown(),
    )

    with pytest.raises(RecoveryRequiredError, match="unknown exchange state"):
        executor.execute(
            OrderIntent("SOLUSDT", OrderIntentType.OPEN, 1, Decimal("1.2"), "decision-3")
        )
    assert len(client.created) == 1
    assert len(client.lookups) == 3


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

    client.open_orders = []
    client.open_algo_orders = [{"algoId": 2}]
    with pytest.raises(UnsupportedAccountError, match="open orders"):
        executor.validate_one_way_account("SOLUSDT")


def test_account_validation_can_return_reconciled_state(rules):
    client = FakeClient()
    client.positions = [{"symbol": "SOLUSDT", "positionAmt": "-2.5"}]
    client.open_orders = [{"orderId": 1}]
    client.open_algo_orders = [{"algoId": 2}]

    result = ExchangeExecutor(client, rules).validate_one_way_account(
        "SOLUSDT", allow_existing_position=True, allow_open_orders=True
    )

    assert result.position_quantity == Decimal("-2.5")
    assert result.open_order_count == 2


@pytest.mark.parametrize("failed_query", ["regular", "algo"])
def test_account_validation_fails_closed_when_any_open_order_query_fails(
    rules, failed_query
):
    client = FakeClient()
    if failed_query == "regular":
        client.open_orders_error = TimeoutError("regular orders timed out")
    else:
        client.open_algo_orders_error = TimeoutError("algo orders timed out")

    with pytest.raises(BinanceGatewayUnavailable):
        ExchangeExecutor(client, rules).validate_one_way_account(
            "SOLUSDT", allow_open_orders=True
        )

    assert client.created == []
    assert client.lookups == []
    assert client.write_calls == []


def test_cross_30x_position_validation_never_writes_to_exchange(rules):
    client = FakeClient()
    client.positions = [{
        "symbol": "SOLUSDT",
        "positionAmt": "10",
        "entryPrice": "88.1085",
        "isolated": False,
        "leverage": "30",
    }]
    executor = ExchangeExecutor(client, rules)

    account = executor.validate_one_way_account(
        "SOLUSDT", allow_existing_position=True, allow_open_orders=True
    )
    with pytest.raises(UnsupportedAccountError, match="isolated"):
        executor.validate_symbol_risk("SOLUSDT")

    assert account.position_quantity == Decimal("10")
    assert client.created == []
    assert client.lookups == []
    assert client.write_calls == []


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


def test_reads_flat_position_risk_from_symbol_config_when_demo_omits_row(rules):
    client = FakeClient()
    client.positions = []
    executor = ExchangeExecutor(client, rules)

    position = executor.validate_symbol_risk("SOLUSDT")

    assert position.direction == 0
    assert position.quantity == Decimal("0")
    assert position.isolated is True
    assert position.leverage == 1
    assert client.read_calls == ["positions", "symbol_config"]


def test_enriches_demo_open_position_with_missing_risk_fields(rules):
    client = FakeClient()
    client.positions = [{
        "symbol": "SOLUSDT",
        "positionAmt": "-1.31",
        "entryPrice": "93.69",
    }]
    executor = ExchangeExecutor(client, rules)

    position = executor.validate_symbol_risk("SOLUSDT")

    assert position.direction == -1
    assert position.quantity == Decimal("1.31")
    assert position.entry_price == Decimal("93.69")
    assert position.isolated is True
    assert position.leverage == 1
    assert client.read_calls == ["positions", "symbol_config"]


def test_flat_symbol_config_still_enforces_margin_and_leverage(rules):
    client = FakeClient()
    client.positions = []
    client.symbol_configs = [{
        "symbol": "SOLUSDT",
        "marginType": "CROSSED",
        "leverage": 20,
    }]

    with pytest.raises(UnsupportedAccountError, match="isolated"):
        ExchangeExecutor(client, rules).validate_symbol_risk("SOLUSDT")


def test_empty_position_and_symbol_config_fail_closed(rules):
    client = FakeClient()
    client.positions = []
    client.symbol_configs = []

    with pytest.raises(ExchangeExecutionError, match="position information"):
        ExchangeExecutor(client, rules).current_position("SOLUSDT")


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
