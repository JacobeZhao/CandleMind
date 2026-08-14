from __future__ import annotations

import pytest

from backend.app.services.paper_broker import PaperBroker
from backend.app.strategies.sar_pyramid import SarPyramidConfig


def test_paper_broker_accounts_for_layers_close_and_fees() -> None:
    config = SarPyramidConfig(fee_rate=0.001, slippage_rate=0.0)
    broker = PaperBroker(10_000.0)
    broker.open(1, 100.0, "d1", config)
    broker.add(110.0, "d2", config)
    fill = broker.close("reverse_close", 120.0, "d3", config)

    assert fill.quantity == pytest.approx(40.0)
    assert broker.snapshot().direction == 0
    assert broker.cash > 10_000.0
    assert broker.fees > 0.0


def test_duplicate_decision_is_rejected_and_funding_is_idempotent() -> None:
    config = SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0)
    broker = PaperBroker(10_000.0)
    broker.open(1, 100.0, "d1", config)
    with pytest.raises(ValueError, match="already processed"):
        broker.add(101.0, "d1", config)
    first = broker.settle_funding("f1", 0.001, 100.0)
    assert first == pytest.approx(-2.0)
    assert broker.settle_funding("f1", 0.001, 100.0) == 0.0


def test_serialized_broker_round_trip_preserves_position() -> None:
    broker = PaperBroker(10_000.0)
    broker.open(-1, 100.0, "d1", SarPyramidConfig())
    recovered = PaperBroker.from_dict(broker.to_dict())
    assert recovered.to_dict() == broker.to_dict()
