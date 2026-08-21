import asyncio

from backend.app.services.market_agent import MarketAgentManager
from backend.app.services.market_agent_queue_store import MarketAgentQueueStore
from backend.app.services.market_agent_state_store import MarketAgentStateStore


class _Client:
    testnet = True


class _Graph:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        cutoff = "2026-08-21T00:04:59.999000Z"
        await kwargs["on_batch_ready"](kwargs["thread_id"], kwargs["thread_id"], cutoff)
        return {
            "batch_id": kwargs["thread_id"],
            "snapshot": {
                "trigger_cutoff": cutoff,
                "intervals": {
                    "5m": {
                        "close": 144.2,
                        "sar": {"direction": "long"},
                        "adx": {"value": 31.0, "plus_di": 34.0, "minus_di": 15.0},
                    }
                },
            },
            "reasons": ["candle_closed", "sar_reversal"],
            "answer": "5分钟转多，但仍需等待高周期确认。",
            "structured": {"regime": "transition", "bias": "long"},
            "summary": {"role": "assistant", "content": "5分钟转多。"},
        }

    async def close(self):
        return None


def _manager(tmp_path, monkeypatch):
    notifications = []

    async def notify(payload):
        notifications.append(payload)

    manager = MarketAgentManager(
        state_store=MarketAgentStateStore(tmp_path / "state"),
        queue_store=MarketAgentQueueStore(tmp_path / "harness.sqlite3"),
        graph=_Graph(),
        client_getter=lambda: _Client(),
        notifier=notify,
        idle_poll_seconds=0.01,
    )
    manager._loaded = True
    manager._generation = 1
    manager._state.update(
        desired_enabled=True,
        agent_id="agent-1",
        symbol="SOLUSDT",
        network="testnet",
        state="running",
    )
    monkeypatch.setattr(
        manager,
        "_resolve_config",
        lambda expected_id: (
            1,
            {
                "provider": "openai",
                "api_key": "secret",
                "base_url": "https://example.invalid/v1",
                "model_name": "test-model",
            },
            None,
        ),
    )
    return manager, notifications


def test_confirmed_close_is_durable_deduplicated_and_publishes_complete_event(
    tmp_path, monkeypatch
):
    manager, notifications = _manager(tmp_path, monkeypatch)
    payload = {
        "network": "testnet",
        "symbol": "SOLUSDT",
        "interval": "5m",
        "close_time": 1787270699999,
    }

    async def scenario():
        await manager.on_closed_kline(payload)
        await manager.on_closed_kline(payload)
        job = manager._claim_fair_job()
        assert job is not None
        await manager._process_queued_job(job, 1)

    asyncio.run(scenario())

    assert len(manager.graph.calls) == 1
    assert manager.graph.calls[0]["cutoff_ms"] == payload["close_time"]
    assert len(notifications) == 1
    event = notifications[0]["data"]
    assert event["content"] == "5分钟转多，但仍需等待高周期确认。"
    assert event["structured"]["bias"] == "long"
    assert event["reasons"] == ["candle_closed", "sar_reversal"]
    assert manager.status()["latest_sequence"] == 1


def test_manual_inbox_has_priority_and_is_idempotent(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)

    async def scenario():
        first = await manager.message(
            symbol="SOLUSDT", content="现在是什么周期？", client_message_id="message-1"
        )
        second = await manager.message(
            symbol="SOLUSDT", content="现在是什么周期？", client_message_id="message-1"
        )
        return first, second, manager._claim_fair_job()

    first, second, job = asyncio.run(scenario())
    assert first["job_id"] == second["job_id"]
    assert job is not None
    assert job.lane.value == "inbox"
