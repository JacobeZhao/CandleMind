from __future__ import annotations

import pytest

from backend.app.services.strategy_runtime_registry import (
    STRATEGY_RUNTIME_REGISTRY,
    get_strategy_runtime,
    registered_strategy_types,
)
from backend.app.strategies.sar_layered import SarLayerMode, transition_sar_layered


def test_registry_exposes_both_layered_strategies() -> None:
    assert registered_strategy_types() == ("sar_martingale", "sar_anti_martingale")
    assert set(STRATEGY_RUNTIME_REGISTRY) == set(registered_strategy_types())


@pytest.mark.parametrize(
    ("strategy_type", "mode"),
    [
        ("sar_martingale", SarLayerMode.MARTINGALE),
        ("sar_anti_martingale", SarLayerMode.ANTI_MARTINGALE),
    ],
)
def test_registry_binds_mode_defaults_and_transition(strategy_type, mode) -> None:
    definition = get_strategy_runtime(strategy_type)
    config = definition.default_config()
    config.validate()
    assert definition.strategy_type == strategy_type
    assert definition.runtime_version == "sar_layered_v1"
    assert definition.transition is transition_sar_layered
    assert config.mode is mode


def test_registry_is_read_only_and_unknown_types_fail_closed() -> None:
    with pytest.raises(TypeError):
        STRATEGY_RUNTIME_REGISTRY["other"] = object()
    with pytest.raises(ValueError, match="unknown strategy type"):
        get_strategy_runtime("other")
