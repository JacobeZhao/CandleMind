from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routes import market_agent as route
from backend.app.services.market_agent import MarketAgentError


class FakeManager:
    def __init__(self):
        self.started_symbols = []
        self.messages = []

    async def start(self, *, symbol, interval=None):
        self.started_symbols.append((symbol, interval))
        if symbol == "BTCUSDT":
            raise MarketAgentError("agent_context_conflict", "conflict", status_code=409)
        return {
            "state": "running",
            "desired_enabled": True,
            "symbol": symbol,
            "trigger_interval": "5m",
        }

    async def stop(self):
        return {"state": "stopped", "desired_enabled": False}

    async def message(self, *, symbol, content):
        self.messages.append((symbol, content))
        return {"type": "assistant_message", "symbol": symbol, "content": "analysis"}

    def status(self):
        return {"state": "running", "desired_enabled": True, "latest_sequence": 3}

    def events(self, *, after_sequence, limit):
        return [{"sequence": 3}] if after_sequence < 3 else []


def _client(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(route, "market_agent_manager", manager)
    monkeypatch.setattr(route.app_state, "exchange_provider", "binance")
    app = FastAPI()
    app.include_router(route.router, prefix="/api/ai")
    return TestClient(app), manager


def test_start_accepts_symbol_only_and_uses_fixed_schedule(monkeypatch):
    client, manager = _client(monkeypatch)

    response = client.post("/api/ai/market-agent/start", json={"symbol": "SOLUSDT"})

    assert response.status_code == 200
    assert response.json()["trigger_interval"] == "5m"
    assert response.json()["desired_enabled"] is True
    assert manager.started_symbols == [("SOLUSDT", None)]


def test_start_tolerates_but_does_not_forward_legacy_interval(monkeypatch):
    client, manager = _client(monkeypatch)

    response = client.post(
        "/api/ai/market-agent/start",
        json={"symbol": "SOLUSDT", "interval": "1h"},
    )

    assert response.status_code == 200
    assert response.json()["trigger_interval"] == "5m"
    assert manager.started_symbols == [("SOLUSDT", None)]


def test_lifecycle_events_validation_and_redacted_errors(monkeypatch):
    client, _ = _client(monkeypatch)

    assert client.post(
        "/api/ai/market-agent/start", json={"symbol": "solusdt"}
    ).status_code == 422
    response = client.post(
        "/api/ai/market-agent/start", json={"symbol": "BTCUSDT"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "agent_context_conflict"
    assert response.json()["detail"]["message"] == "conflict"
    assert client.get("/api/ai/market-agent/events?after_sequence=2").json() == {
        "events": [{"sequence": 3}],
        "latest_sequence": 3,
        "provider": "binance",
    }
    assert client.get("/api/ai/market-agent/events?limit=101").status_code == 422
    assert client.post("/api/ai/market-agent/stop").json() == {
        "state": "stopped",
        "desired_enabled": False,
    }


def test_manual_message_uses_the_persistent_agent_contract(monkeypatch):
    client, manager = _client(monkeypatch)

    response = client.post(
        "/api/ai/market-agent/messages",
        json={"symbol": "SOLUSDT", "content": "  Analyze the current regime  "},
    )

    assert response.status_code == 200
    assert response.json()["type"] == "assistant_message"
    assert manager.messages == [("SOLUSDT", "Analyze the current regime")]


def test_non_binance_blocks_start_and_messages_but_keeps_stop_and_status(monkeypatch):
    client, manager = _client(monkeypatch)
    route.app_state.exchange_provider = "okx"

    start = client.post("/api/ai/market-agent/start", json={"symbol": "SOLUSDT"})
    message = client.post(
        "/api/ai/market-agent/messages",
        json={"symbol": "SOLUSDT", "content": "Analyze"},
    )

    for response in (start, message):
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "exchange_provider_unavailable",
            "message": "所选市场暂未接入，敬请期待。",
            "retryable": False,
            "provider": "okx",
        }
    assert manager.started_symbols == []
    assert manager.messages == []
    assert client.get("/api/ai/market-agent/status").json()["provider"] == "okx"
    assert client.get("/api/ai/market-agent/events").json()["provider"] == "okx"
    assert client.post("/api/ai/market-agent/stop").status_code == 200
