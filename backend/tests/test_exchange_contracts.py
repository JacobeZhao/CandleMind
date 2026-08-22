from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.app.exchanges.binance.adapter import build_binance_adapter
from backend.app.exchanges.binance.adapter import BinanceAdapterError
from backend.app.exchanges.contracts import (
    AccountPort,
    ExchangeBinding,
    ExchangeNetwork,
    ExecutionAuthorization,
    ExecutionAuthorizationError,
    MarketDataPort,
    OrderAction,
    OrderRequest,
    TradingPort,
)


class GatewayStub:
    client = "must-not-be-exposed"

    def server_time(self):
        return 2_000

    def ticker(self, **params):
        assert params == {"symbol": "SOLUSDT"}
        return {
            "symbol": "SOLUSDT",
            "lastPrice": "150.5",
            "highPrice": "155",
            "lowPrice": "145",
            "closeTime": 1_900,
        }

    def klines(self, **params):
        assert params == {"symbol": "SOLUSDT", "interval": "5m", "limit": 2}
        return [
            [1_000, "100", "110", "90", "105", "12", 1_500],
            [1_500, "105", "115", "100", "110", "8", 2_000],
        ]


class ExecutorStub:
    client = "must-not-be-exposed"

    def __init__(self):
        self.execute_calls = []
        self.lookup_calls = []

    def available_balance(self, asset):
        assert asset == "USDT"
        return Decimal("250.25")

    def current_position(self, symbol):
        assert symbol == "SOLUSDT"
        return SimpleNamespace(
            symbol=symbol,
            direction=1,
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
            isolated=True,
            leverage=1,
        )

    def execute(self, intent):
        self.execute_calls.append(intent)
        return self._result(intent)

    def lookup(self, intent):
        self.lookup_calls.append(intent)
        result = self._result(intent)
        result.recovered_after_ambiguous_submit = True
        return result

    @staticmethod
    def _result(intent):
        return SimpleNamespace(
            symbol=intent.symbol,
            action=intent.action,
            status="FILLED",
            side="BUY",
            quantity=intent.quantity,
            executed_quantity=intent.quantity,
            average_price=Decimal("101"),
            order_id=42,
            client_order_id="cm-test",
            recovered_after_ambiguous_submit=False,
            raw={"apiKey": "secret"},
        )


@pytest.fixture
def binding():
    return ExchangeBinding("binance", ExchangeNetwork.TESTNET, "SOLUSDT")


@pytest.fixture
def adapter(binding):
    return build_binance_adapter(binding, GatewayStub(), ExecutorStub())


def test_binance_adapter_satisfies_ports_and_normalizes_market_data(adapter):
    assert isinstance(adapter.market, MarketDataPort)
    assert isinstance(adapter.account, AccountPort)
    assert isinstance(adapter.trading, TradingPort)

    ticker = adapter.market.ticker()
    klines = adapter.market.completed_klines("5m", 2)

    assert ticker.last_price == Decimal("150.5")
    assert len(klines) == 1
    assert klines[0].closed is True
    assert not hasattr(adapter.market, "client")
    assert not hasattr(ticker, "raw")


def test_account_and_order_results_do_not_expose_sdk_or_raw_payload(adapter, binding):
    balance = adapter.account.available_balance()
    position = adapter.account.current_position()
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")
    result = adapter.trading.execute(request, ExecutionAuthorization.issue(binding))

    assert balance.available == Decimal("250.25")
    assert position.isolated is True
    assert result.order_id == "42"
    assert result.action is OrderAction.OPEN
    assert not hasattr(result, "raw")
    assert not hasattr(adapter.trading, "client")


def test_testnet_authorization_rejects_provider_network_and_symbol_mismatch(adapter, binding):
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")
    mismatches = [
        ExchangeBinding("okx", "testnet", "SOLUSDT"),
        ExchangeBinding("binance", "mainnet", "SOLUSDT"),
        ExchangeBinding("binance", "testnet", "BTCUSDT"),
    ]

    for mismatch in mismatches:
        authorization = object.__new__(ExecutionAuthorization)
        object.__setattr__(authorization, "binding", mismatch)
        with pytest.raises(ExecutionAuthorizationError, match="does not match"):
            adapter.trading.execute(request, authorization)


def test_order_symbol_must_match_authorized_adapter_scope(adapter, binding):
    request = OrderRequest("BTCUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")

    with pytest.raises(ValueError, match="order symbol"):
        adapter.trading.execute(request, ExecutionAuthorization.issue(binding))


def test_mainnet_authorization_requires_server_enablement(monkeypatch):
    binding = ExchangeBinding("binance", "mainnet", "SOLUSDT")
    monkeypatch.delenv("CANDLEMIND_MAINNET_TRADING_ENABLED", raising=False)

    with pytest.raises(ExecutionAuthorizationError, match="disabled"):
        ExecutionAuthorization.issue(binding)

    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "true")
    authorization = ExecutionAuthorization.issue(binding)
    adapter = build_binance_adapter(binding, GatewayStub(), ExecutorStub())
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")

    assert adapter.trading.execute(request, authorization).status == "FILLED"


def test_mainnet_authorization_is_revoked_when_server_switch_turns_off(monkeypatch):
    binding = ExchangeBinding("binance", "mainnet", "SOLUSDT")
    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "1")
    authorization = ExecutionAuthorization.issue(binding)
    adapter = build_binance_adapter(binding, GatewayStub(), ExecutorStub())
    monkeypatch.setenv("CANDLEMIND_MAINNET_TRADING_ENABLED", "false")

    with pytest.raises(ExecutionAuthorizationError, match="disabled"):
        adapter.trading.execute(
            OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1"),
            authorization,
        )


def test_mainnet_gate_blocks_execute_and_lookup_before_executor_call(monkeypatch):
    binding = ExchangeBinding("binance", "mainnet", "SOLUSDT")
    executor = ExecutorStub()
    adapter = build_binance_adapter(binding, GatewayStub(), executor)
    authorization = ExecutionAuthorization(binding)
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")
    monkeypatch.delenv("CANDLEMIND_MAINNET_TRADING_ENABLED", raising=False)

    with pytest.raises(ExecutionAuthorizationError, match="disabled"):
        adapter.trading.execute(request, authorization)
    with pytest.raises(ExecutionAuthorizationError, match="disabled"):
        adapter.trading.lookup(request, authorization)

    assert executor.execute_calls == []
    assert executor.lookup_calls == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"direction": 0}, "direction"),
        ({"quantity": Decimal("0")}, "quantity"),
        ({"quantity": Decimal("NaN")}, "quantity"),
        ({"decision_id": "  "}, "decision ID"),
        ({"ordinal": -1}, "ordinal"),
        ({"ordinal": True}, "ordinal"),
    ],
)
def test_order_request_rejects_invalid_execution_values(changes, message):
    values = {
        "symbol": "SOLUSDT",
        "action": OrderAction.OPEN,
        "direction": 1,
        "quantity": Decimal("1"),
        "decision_id": "d-1",
        "ordinal": 0,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        OrderRequest(**values)


def test_order_result_must_match_bound_symbol(binding):
    class WrongSymbolExecutor(ExecutorStub):
        @staticmethod
        def _result(intent):
            result = ExecutorStub._result(intent)
            result.symbol = "BTCUSDT"
            return result

    adapter = build_binance_adapter(binding, GatewayStub(), WrongSymbolExecutor())
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")

    with pytest.raises(BinanceAdapterError, match="symbol"):
        adapter.trading.execute(request, ExecutionAuthorization.issue(binding))


def test_account_position_must_match_bound_symbol(binding):
    class WrongPositionExecutor(ExecutorStub):
        def current_position(self, symbol):
            position = super().current_position(symbol)
            position.symbol = "BTCUSDT"
            return position

    adapter = build_binance_adapter(binding, GatewayStub(), WrongPositionExecutor())

    with pytest.raises(BinanceAdapterError, match="symbol"):
        adapter.account.current_position()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", None, "order status"),
        ("client_order_id", None, "client order ID"),
        ("recovered_after_ambiguous_submit", "false", "reconciliation flag"),
    ],
)
def test_order_result_rejects_values_that_cannot_be_safely_normalized(
    binding, field, value, message
):
    class MalformedExecutor(ExecutorStub):
        @staticmethod
        def _result(intent):
            result = ExecutorStub._result(intent)
            setattr(result, field, value)
            return result

    adapter = build_binance_adapter(binding, GatewayStub(), MalformedExecutor())
    request = OrderRequest("SOLUSDT", OrderAction.OPEN, 1, Decimal("1"), "d-1")

    with pytest.raises(BinanceAdapterError, match=message):
        adapter.trading.execute(request, ExecutionAuthorization.issue(binding))
