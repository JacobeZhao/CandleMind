"""Registry metadata for production strategy runtime integration."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from backend.app.strategies.sar_layered import (
    SarLayerMode,
    SarLayeredConfig,
    transition_sar_layered,
)


@dataclass(frozen=True, slots=True)
class StrategyRuntimeDefinition:
    strategy_type: str
    runtime_version: str
    display_name: str
    default_config: Callable[[], SarLayeredConfig]
    transition: Callable = transition_sar_layered


def _martingale_config() -> SarLayeredConfig:
    return SarLayeredConfig(mode=SarLayerMode.MARTINGALE)


def _anti_martingale_config() -> SarLayeredConfig:
    return SarLayeredConfig(mode=SarLayerMode.ANTI_MARTINGALE)


STRATEGY_RUNTIME_REGISTRY: Mapping[str, StrategyRuntimeDefinition] = MappingProxyType(
    {
        "sar_martingale": StrategyRuntimeDefinition(
            strategy_type="sar_martingale",
            runtime_version="sar_layered_v1",
            display_name="SAR Martingale",
            default_config=_martingale_config,
        ),
        "sar_anti_martingale": StrategyRuntimeDefinition(
            strategy_type="sar_anti_martingale",
            runtime_version="sar_layered_v1",
            display_name="SAR Anti-Martingale",
            default_config=_anti_martingale_config,
        ),
    }
)


def get_strategy_runtime(strategy_type: str) -> StrategyRuntimeDefinition:
    try:
        return STRATEGY_RUNTIME_REGISTRY[strategy_type]
    except KeyError as exc:
        raise ValueError(f"unknown strategy type: {strategy_type}") from exc


def registered_strategy_types() -> tuple[str, ...]:
    return tuple(STRATEGY_RUNTIME_REGISTRY)
