import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base, StrategyConfiguration
from backend.app.services.strategy_configuration import (
    StrategyConfigurationConflict,
    canonical_json,
    configuration_hash,
    default_configuration,
    get_strategy_configuration,
    save_strategy_configuration,
    strategy_catalog,
    validated_parameters,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_catalog_exposes_three_strategies_with_fixed_five_minute_defaults():
    catalog = strategy_catalog()

    assert [item["strategy_type"] for item in catalog] == [
        "sar_adx_trend",
        "sar_martingale",
        "sar_anti_martingale",
    ]
    assert all(
        item["default_parameters"]["execution_interval"] == "5m"
        for item in catalog
    )
    assert all("properties" in item["parameter_schema"] for item in catalog)
    interval_schema = catalog[1]["parameter_schema"]["properties"][
        "execution_interval"
    ]
    assert interval_schema["const"] == "5m"


def test_default_is_persisted_once_and_has_reproducible_hash(db):
    first = get_strategy_configuration(db)
    second = get_strategy_configuration(db)

    assert first == second
    assert first["strategy_type"] == "sar_adx_trend"
    assert db.query(StrategyConfiguration).count() == 1
    assert first["config_hash"] == configuration_hash(
        first["strategy_type"], first["config_version"], first["parameters"]
    )
    assert json.loads(canonical_json(first["parameters"])) == first["parameters"]


@pytest.mark.parametrize("strategy_type", ["sar_martingale", "sar_anti_martingale"])
def test_layered_strategy_round_trip_normalizes_parameters(db, strategy_type):
    parameters = default_configuration(strategy_type)["parameters"]
    parameters.update({"max_layers": 6, "layer_multiplier": 1.8})

    saved = save_strategy_configuration(
        db, strategy_type=strategy_type, parameters=parameters
    )

    assert saved["strategy_type"] == strategy_type
    assert saved["parameters"]["max_layers"] == 6
    assert saved["parameters"]["layer_multiplier"] == 1.8
    assert get_strategy_configuration(db) == saved


@pytest.mark.parametrize(
    "parameters",
    [
        {"execution_interval": "15m"},
        {"sar_step": 0.03, "sar_max": 0.02},
        {"unknown_parameter": True},
    ],
)
def test_invalid_or_unknown_parameters_are_rejected(parameters):
    with pytest.raises(ValidationError):
        validated_parameters("sar_martingale", parameters)


def test_stale_hash_cannot_overwrite_configuration(db):
    current = get_strategy_configuration(db)
    parameters = default_configuration("sar_martingale")["parameters"]

    with pytest.raises(StrategyConfigurationConflict, match="reload"):
        save_strategy_configuration(
            db,
            strategy_type="sar_martingale",
            parameters=parameters,
            expected_config_hash="0" * 64,
        )

    assert get_strategy_configuration(db) == current


def test_tampered_stored_hash_is_rejected(db):
    get_strategy_configuration(db)
    record = db.get(StrategyConfiguration, 1)
    record.config_hash = "0" * 64
    db.commit()

    with pytest.raises(StrategyConfigurationConflict, match="hash"):
        get_strategy_configuration(db)


def test_malformed_stored_parameters_are_rejected(db):
    get_strategy_configuration(db)
    record = db.get(StrategyConfiguration, 1)
    record.parameters_json = "not-json"
    db.commit()

    with pytest.raises(StrategyConfigurationConflict, match="parameters"):
        get_strategy_configuration(db)
