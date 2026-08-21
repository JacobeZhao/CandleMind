import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.database import Settings
from backend.app.routes import strategy as strategy_routes


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Settings(id=1, testnet=True, symbol="SOLUSDT"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_catalog_route_returns_three_configurable_strategies():
    result = strategy_routes.get_strategy_catalog()
    assert len(result["strategies"]) == 3


def test_config_routes_save_and_load_backend_authoritative_config(monkeypatch, db):
    monkeypatch.setattr(
        strategy_routes,
        "bot_engine",
        SimpleNamespace(running=False, engine_state="stopped", has_execution_journal=lambda *_: False),
    )
    current = strategy_routes.get_saved_strategy_configuration(db)
    body = strategy_routes.StrategyConfigurationUpdate(
        strategy_type="sar_martingale",
        parameters={
            "execution_interval": "5m",
            "sar_step": 0.02,
            "sar_max": 0.2,
            "max_layers": 4,
            "layer_multiplier": 1.5,
            "add_trigger_fraction": 0.005,
        },
        expected_config_hash=current["config_hash"],
    )

    saved = asyncio.run(strategy_routes.update_strategy_configuration(body, db))

    assert saved["strategy_type"] == "sar_martingale"
    assert strategy_routes.get_saved_strategy_configuration(db) == saved


def test_running_strategy_rejects_configuration_change(monkeypatch, db):
    monkeypatch.setattr(strategy_routes, "bot_engine", SimpleNamespace(running=True))
    body = strategy_routes.StrategyConfigurationUpdate(
        strategy_type="sar_anti_martingale",
        parameters={
            "execution_interval": "5m",
            "sar_step": 0.02,
            "sar_max": 0.2,
            "max_layers": 4,
            "layer_multiplier": 1.5,
            "add_trigger_fraction": 0.005,
        },
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(strategy_routes.update_strategy_configuration(body, db))

    assert raised.value.status_code == 409
    assert "Stop" in raised.value.detail
