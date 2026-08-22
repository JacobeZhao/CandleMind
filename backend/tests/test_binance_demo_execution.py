"""Opt-in Binance Demo write validation; skipped unless explicitly authorized."""

from decimal import Decimal
import os

import pytest
from binance.client import Client

from backend.app.services.binance_usdm_gateway import BinanceUsdMGateway
from backend.app.services.exchange_executor import (
    ExchangeExecutor,
    OrderIntent,
    OrderIntentType,
    SymbolRules,
)


WRITE_CONFIRMATION = "PLACE_AND_CLOSE_BINANCE_DEMO_ORDER"


@pytest.mark.skipif(
    os.environ.get("CANDLEMIND_BINANCE_DEMO_WRITE_TEST") != WRITE_CONFIRMATION,
    reason="Binance Demo writes require explicit opt-in",
)
def test_binance_demo_market_order_fills_and_is_reconciled() -> None:
    api_key = os.environ.get("BINANCE_DEMO_API_KEY")
    api_secret = os.environ.get("BINANCE_DEMO_API_SECRET")
    if not api_key or not api_secret:
        pytest.fail("BINANCE_DEMO_API_KEY and BINANCE_DEMO_API_SECRET are required")

    symbol = os.environ.get("BINANCE_DEMO_SYMBOL", "SOLUSDT").strip().upper()
    client = Client(api_key, api_secret, testnet=True)
    if "demo-fapi.binance.com" not in client.FUTURES_URL:
        pytest.fail("refusing write because the client is not bound to Binance Demo")

    gateway = BinanceUsdMGateway(client)
    exchange_info = gateway.exchange_info()
    executor = ExchangeExecutor(
        client, SymbolRules.from_exchange_info(exchange_info, symbol), "testnet"
    )
    executor.validate_one_way_account(symbol)
    position = executor.validate_symbol_risk(symbol)
    if position.direction:
        pytest.fail("Demo account must be flat before this validation")

    mark = Decimal(str(gateway.mark_price(symbol=symbol)["markPrice"]))
    quantity = executor.layer_quantity(
        available_balance=executor.available_balance(),
        capital_limit=Decimal("20"),
        reference_price=mark,
        layers=1,
    )
    decision_id = "explicit-binance-demo-validation"
    opened = None
    try:
        opened = executor.execute(
            OrderIntent(symbol, OrderIntentType.OPEN, 1, quantity, decision_id, 0)
        )
        assert opened.status == "FILLED"
        assert opened.executed_quantity == opened.quantity
        assert executor.lookup(
            OrderIntent(symbol, OrderIntentType.OPEN, 1, quantity, decision_id, 0)
        ).status == "FILLED"
    finally:
        current = executor.current_position(symbol)
        if current.direction:
            closed = executor.execute(
                OrderIntent(
                    symbol,
                    OrderIntentType.CLOSE,
                    current.direction,
                    current.quantity,
                    f"{decision_id}-cleanup",
                    0,
                )
            )
            assert closed.status == "FILLED"
