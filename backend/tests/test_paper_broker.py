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
    assert broker.paper_fill_count == 3
    assert broker.paper_fill_count_complete is True


def test_duplicate_decision_is_rejected_and_funding_is_idempotent() -> None:
    config = SarPyramidConfig(fee_rate=0.0, slippage_rate=0.0)
    broker = PaperBroker(10_000.0)
    broker.open(1, 100.0, "d1", config)
    with pytest.raises(ValueError, match="already processed"):
        broker.add(101.0, "d1", config)
    assert broker.paper_fill_count == 1
    first = broker.settle_funding("f1", 0.001, 100.0)
    assert first == pytest.approx(-2.0)
    assert broker.settle_funding("f1", 0.001, 100.0) == 0.0


def test_serialized_broker_round_trip_preserves_position() -> None:
    broker = PaperBroker(10_000.0)
    broker.open(-1, 100.0, "d1", SarPyramidConfig())
    recovered = PaperBroker.from_dict(broker.to_dict())
    assert recovered.to_dict() == broker.to_dict()
    assert recovered.paper_fill_count == 1
    assert recovered.paper_fill_count_complete is True


def test_v1_state_with_processed_decisions_has_complete_fill_count() -> None:
    payload = PaperBroker(10_000.0).to_dict()
    payload.pop("paper_fill_count_complete")
    payload["processed_decisions"] = ["d1", "d2"]

    recovered = PaperBroker.from_dict(payload)

    assert recovered.paper_fill_count == 2
    assert recovered.paper_fill_count_complete is True
    assert recovered.to_dict()["paper_fill_count_complete"] is True


def test_legacy_state_without_processed_decisions_marks_fill_count_incomplete() -> None:
    payload = PaperBroker(10_000.0).to_dict()
    payload.pop("paper_fill_count_complete")
    payload.pop("processed_decisions")

    recovered = PaperBroker.from_dict(payload)

    assert recovered.paper_fill_count == 0
    assert recovered.paper_fill_count_complete is False
    assert recovered.to_dict()["paper_fill_count_complete"] is False


def test_invalid_fill_count_completeness_fails_closed() -> None:
    payload = PaperBroker(10_000.0).to_dict()
    payload["paper_fill_count_complete"] = "false"

    with pytest.raises(ValueError, match="completeness"):
        PaperBroker.from_dict(payload)
