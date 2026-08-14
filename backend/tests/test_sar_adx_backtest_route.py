from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.routes import backtest
from backend.app.services.sar_adx_backtest import (
    SarAdxDataUnavailableError,
    SarAdxWindowError,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(backtest.router, prefix="/api/backtest")
    return TestClient(app)


def _request(**overrides):
    payload = {
        "start_date": "2025-01-01",
        "end_date": "2025-02-01",
        "initial_capital": 10_000,
        "fee_rate": 0.001,
        "slippage_bps": 2,
    }
    payload.update(overrides)
    return payload


def test_route_runs_offline_service_without_database_or_binance(client, monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"strategy": {"id": "sar_adx_pyramid_v3"}}

    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.run_sar_adx_backtest", fake_run
    )
    response = client.post("/api/backtest/sar-adx", json=_request())

    assert response.status_code == 200
    assert response.json()["strategy"]["id"] == "sar_adx_pyramid_v3"
    assert captured["initial_capital"] == 10_000
    assert captured["slippage_bps"] == 2
    assert captured["symbol"] == "SOLUSDT"


def test_route_accepts_verified_symbol_shape(client, monkeypatch) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"strategy": {"symbol": kwargs["symbol"]}}

    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.run_sar_adx_backtest", fake_run
    )
    response = client.post(
        "/api/backtest/sar-adx", json=_request(symbol="BTCUSDT")
    )

    assert response.status_code == 200
    assert response.json()["strategy"]["symbol"] == "BTCUSDT"
    assert captured["symbol"] == "BTCUSDT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "../../SOLUSDT"},
        {"start_date": "2025-02-01", "end_date": "2025-01-01"},
        {"start_date": "2023-01-01", "end_date": "2025-01-02"},
        {"initial_capital": 0},
        {"fee_rate": 0.02},
        {"slippage_bps": 101},
    ],
)
def test_route_rejects_invalid_contract(client, overrides) -> None:
    response = client.post("/api/backtest/sar-adx", json=_request(**overrides))
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (SarAdxWindowError("outside verified coverage"), 422),
        (SarAdxDataUnavailableError("verified release unavailable"), 503),
    ],
)
def test_route_maps_safe_service_errors(client, monkeypatch, error, status) -> None:
    def fake_run(**_kwargs):
        raise error

    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.run_sar_adx_backtest", fake_run
    )
    response = client.post("/api/backtest/sar-adx", json=_request())

    assert response.status_code == status
    assert "G:\\" not in response.text


def test_route_rejects_concurrent_run(client) -> None:
    async def hold_lock():
        await backtest._sar_adx_lock.acquire()

    asyncio.run(hold_lock())
    try:
        response = client.post("/api/backtest/sar-adx", json=_request())
        assert response.status_code == 409
    finally:
        backtest._sar_adx_lock.release()


def test_cancellation_holds_lock_until_backtest_worker_finishes(monkeypatch) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_run(**_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        return {"strategy": {"id": "sar_adx_pyramid_v3"}}

    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.run_sar_adx_backtest",
        blocking_run,
    )

    async def exercise_cancellation() -> None:
        request = backtest.SarAdxBacktestRequest(**_request())
        caller = asyncio.create_task(backtest.sar_adx_backtest(request))
        try:
            assert await asyncio.to_thread(worker_started.wait, 2)

            caller.cancel()
            await asyncio.sleep(0)

            assert backtest._sar_adx_lock.locked()
            assert not caller.done()

            release_worker.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=2)
            assert not backtest._sar_adx_lock.locked()
        finally:
            release_worker.set()
            if not caller.done():
                caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

    asyncio.run(exercise_cancellation())


def test_capabilities_route_returns_verified_contract(client, monkeypatch) -> None:
    expected = {
        "symbols": ["BTCUSDT", "SOLUSDT"],
        "symbol_count": 2,
        "coverage": [],
    }
    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.get_sar_adx_capabilities",
        lambda: expected,
    )

    response = client.get("/api/backtest/sar-adx/capabilities")

    assert response.status_code == 200
    assert response.json() == expected


def test_capabilities_route_redacts_release_path(client, monkeypatch) -> None:
    def unavailable():
        raise SarAdxDataUnavailableError("verified release unavailable")

    monkeypatch.setattr(
        "backend.app.services.sar_adx_backtest.get_sar_adx_capabilities",
        unavailable,
    )
    response = client.get("/api/backtest/sar-adx/capabilities")

    assert response.status_code == 503
    assert "G:\\" not in response.text
