"""Validated, backend-authoritative strategy configuration persistence."""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from ..database import StrategyConfiguration


StrategyType = Literal[
    "sar_adx_trend",
    "sar_martingale",
    "sar_anti_martingale",
]

STRATEGY_VERSIONS: dict[str, str] = {
    "sar_adx_trend": "sar_adx_trend_v1",
    "sar_martingale": "sar_martingale_v1",
    "sar_anti_martingale": "sar_anti_martingale_v1",
}


class CommonParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_interval: Literal["5m"] = "5m"
    sar_step: float = Field(default=0.02, ge=0.001, le=0.1)
    sar_max: float = Field(default=0.2, ge=0.001, le=0.5)
    max_layers: int = Field(default=5, ge=1, le=6)

    @model_validator(mode="after")
    def validate_sar_acceleration(self):
        if self.sar_max < self.sar_step:
            raise ValueError("sar_max must be greater than or equal to sar_step")
        return self


class TrendParameters(CommonParameters):
    max_layers: int = Field(default=5, ge=1, le=6)
    adx_timeframe: Literal["1h"] = "1h"
    adx_period: int = Field(default=14, ge=2, le=100)
    adx_threshold: float = Field(default=45.0, ge=0.0, le=100.0)
    adx_rising_periods: int = Field(default=2, ge=0, le=20)
    entry_confirmation_bars: int = Field(default=6, ge=1, le=50)
    recapture_buffer_fraction: float = Field(default=0.0024, ge=0.0, le=0.05)
    max_entries_per_adx_regime: int = Field(default=2, ge=1, le=20)


class LayeredSarParameters(CommonParameters):
    max_layers: int = Field(default=4, ge=1, le=6)
    layer_multiplier: float = Field(default=1.5, ge=1.0, le=1.8)
    add_trigger_fraction: float = Field(default=0.005, ge=0.001, le=0.05)


PARAMETER_MODELS: dict[str, type[CommonParameters]] = {
    "sar_adx_trend": TrendParameters,
    "sar_martingale": LayeredSarParameters,
    "sar_anti_martingale": LayeredSarParameters,
}

STRATEGY_CATALOG = (
    {
        "strategy_type": "sar_adx_trend",
        "name": "CandleMind Trend Strategy",
        "description": "Trend-following strategy with higher-timeframe market filtering.",
    },
    {
        "strategy_type": "sar_martingale",
        "name": "SAR Martingale",
        "description": "Adds normalized position layers after adverse price movement.",
    },
    {
        "strategy_type": "sar_anti_martingale",
        "name": "SAR Anti-Martingale",
        "description": "Adds normalized position layers after favorable price movement.",
    },
)


class StrategyConfigurationConflict(RuntimeError):
    pass


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def configuration_hash(
    strategy_type: str, config_version: str, parameters: dict[str, Any]
) -> str:
    payload = {
        "config_version": config_version,
        "parameters": parameters,
        "strategy_type": strategy_type,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validated_parameters(
    strategy_type: str, parameters: dict[str, Any] | None = None
) -> dict[str, Any]:
    model = PARAMETER_MODELS.get(strategy_type)
    if model is None:
        raise ValueError(f"unsupported strategy_type: {strategy_type}")
    return model.model_validate(parameters or {}).model_dump(mode="json")


def default_configuration(strategy_type: str = "sar_adx_trend") -> dict[str, Any]:
    parameters = validated_parameters(strategy_type)
    config_version = STRATEGY_VERSIONS[strategy_type]
    return {
        "strategy_type": strategy_type,
        "config_version": config_version,
        "parameters": parameters,
        "config_hash": configuration_hash(strategy_type, config_version, parameters),
    }


def strategy_catalog() -> list[dict[str, Any]]:
    result = []
    for item in STRATEGY_CATALOG:
        strategy_type = item["strategy_type"]
        entry = deepcopy(item)
        entry.update(
            {
                "config_version": STRATEGY_VERSIONS[strategy_type],
                "default_parameters": validated_parameters(strategy_type),
                "parameter_schema": PARAMETER_MODELS[strategy_type].model_json_schema(),
            }
        )
        result.append(entry)
    return result


def _record_payload(record: StrategyConfiguration) -> dict[str, Any]:
    expected_version = STRATEGY_VERSIONS.get(record.strategy_type)
    if expected_version is None or record.config_version != expected_version:
        raise StrategyConfigurationConflict("stored strategy configuration identity is invalid")
    try:
        raw_parameters = json.loads(record.parameters_json)
        parameters = validated_parameters(record.strategy_type, raw_parameters)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StrategyConfigurationConflict(
            "stored strategy configuration parameters are invalid"
        ) from exc
    expected_hash = configuration_hash(
        record.strategy_type, record.config_version, parameters
    )
    if record.config_hash != expected_hash:
        raise StrategyConfigurationConflict("stored strategy configuration hash is invalid")
    return {
        "strategy_type": record.strategy_type,
        "config_version": record.config_version,
        "parameters": parameters,
        "config_hash": record.config_hash,
        "updated_at_ms": record.updated_at_ms,
    }


def ensure_strategy_configuration(db: Session) -> dict[str, Any]:
    record = db.get(StrategyConfiguration, 1)
    if record is None:
        payload = default_configuration()
        record = StrategyConfiguration(
            id=1,
            strategy_type=payload["strategy_type"],
            config_version=payload["config_version"],
            parameters_json=canonical_json(payload["parameters"]),
            config_hash=payload["config_hash"],
            updated_at_ms=int(time.time() * 1000),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
    return _record_payload(record)


def get_strategy_configuration(db: Session) -> dict[str, Any]:
    return ensure_strategy_configuration(db)


def save_strategy_configuration(
    db: Session,
    *,
    strategy_type: str,
    parameters: dict[str, Any],
    expected_config_hash: str | None = None,
) -> dict[str, Any]:
    current = ensure_strategy_configuration(db)
    if expected_config_hash is not None and expected_config_hash != current["config_hash"]:
        raise StrategyConfigurationConflict(
            "strategy configuration changed; reload before saving"
        )

    normalized = validated_parameters(strategy_type, parameters)
    config_version = STRATEGY_VERSIONS[strategy_type]
    config_hash = configuration_hash(strategy_type, config_version, normalized)
    record = db.get(StrategyConfiguration, 1)
    record.strategy_type = strategy_type
    record.config_version = config_version
    record.parameters_json = canonical_json(normalized)
    record.config_hash = config_hash
    record.updated_at_ms = int(time.time() * 1000)
    db.commit()
    db.refresh(record)
    return _record_payload(record)
