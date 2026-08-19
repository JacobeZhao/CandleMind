import asyncio

import pytest

from backend.app.services import market_agent
from backend.app.services.market_agent import MarketAgentError
from backend.app.services.market_agent_state_store import (
    ANALYSIS_INTERVALS,
    MarketAgentStateStore,
)


def _graph_result(
    batch_id="SOLUSDT:2026-08-19T10:04:59.999000Z",
    cutoff="2026-08-19T10:04:59.999000Z",
):
    five_minute = {
        "close": 144.2,
        "sar": {"direction": "long"},
        "adx": {"value": 31.0, "plus_di": 34.0, "minus_di": 15.0},
    }
    return {
        "batch_id": batch_id,
        "snapshot": {
            "trigger_cutoff": cutoff,
            "intervals": {"5m": five_minute},
        },
        "reasons": ["candle_closed"],
        "answer": "Trend analysis",
        "summary": {
            "role": "assistant",
            "content": "Trend analysis",
            "batch_id": batch_id,
            "cutoff": cutoff,
        },
    }


class FakeGraph:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [_graph_result()])
        self.calls = []
        self.closed = False

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        await kwargs["on_batch_ready"](
            outcome["batch_id"], kwargs["thread_id"], outcome["snapshot"]["trigger_cutoff"]
        )
        return outcome

    async def close(self):
        self.closed = True


def _manager(tmp_path, monkeypatch, *, graph=None, notifier=None, daily_limit=300):
    manager = market_agent.MarketAgentManager(
        state_store=MarketAgentStateStore(tmp_path / "agents"),
        graph=graph or FakeGraph(),
        client_getter=lambda: object(),
        notifier=notifier or (lambda payload: asyncio.sleep(0)),
        daily_limit=daily_limit,
        retry_delays=(1, 2, 4),
        idle_poll_seconds=0.1,
    )
    manager._loaded = True
    manager._state.update(
        desired_enabled=True,
        agent_id="agent-1",
        symbol="SOLUSDT",
        config_id=7,
        state="running",
    )
    manager._generation = 1
    monkeypatch.setattr(
        manager,
        "_resolve_config",
        lambda expected_id: (
            7,
            {
                "provider": "openai",
                "api_key": "secret",
                "base_url": "https://api.openai.com/v1",
                "model_name": "test-model",
            },
            None,
        ),
    )
    return manager


def test_status_exposes_fixed_5m_schedule_and_all_six_timeframes(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)

    status = manager.status()

    assert status["trigger_interval"] == "5m"
    assert tuple(status["analysis_intervals"]) == tuple(ANALYSIS_INTERVALS)
    assert status["desired_enabled"] is True


def test_explicit_start_is_immediate_but_restore_waits_for_boundary(tmp_path, monkeypatch):
    start_manager = _manager(tmp_path / "start", monkeypatch)
    start_manager._state.update(desired_enabled=False, state="stopped", symbol=None)
    calls = []

    async def record_run(generation, *, immediate):
        calls.append(immediate)

    monkeypatch.setattr(start_manager, "_run", record_run)

    async def start_scenario():
        await start_manager.start(symbol="SOLUSDT")
        await asyncio.sleep(0)
        await start_manager.shutdown()

    asyncio.run(start_scenario())
    assert calls == [True]

    store = MarketAgentStateStore(tmp_path / "restore" / "agents")
    persisted = market_agent.MarketAgentManager._empty_state()
    persisted.update(
        desired_enabled=True,
        agent_id="persisted-agent",
        symbol="SOLUSDT",
        state="running",
        last_scheduled_cutoff="2026-08-19T10:04:59.999000Z",
    )
    store.save(persisted)
    restore_manager = market_agent.MarketAgentManager(
        state_store=store,
        graph=FakeGraph(),
        client_getter=lambda: object(),
    )
    restored_calls = []

    async def record_restore(generation, *, immediate):
        restored_calls.append(immediate)

    monkeypatch.setattr(restore_manager, "_run", record_restore)

    async def restore_scenario():
        await restore_manager.restore()
        await asyncio.sleep(0)
        await restore_manager.shutdown()

    asyncio.run(restore_scenario())
    assert restored_calls == [False]


def test_duplicate_batch_is_not_committed_or_broadcast_twice(tmp_path, monkeypatch):
    notifications = []

    async def notify(payload):
        notifications.append(payload)

    graph = FakeGraph([_graph_result(), _graph_result()])
    manager = _manager(tmp_path, monkeypatch, graph=graph, notifier=notify)

    async def scenario():
        first = await manager._process_batch(object(), 1, mode="automatic")
        second = await manager._process_batch(object(), 1, mode="automatic")
        return first, second

    first, second = asyncio.run(scenario())

    assert first["content"] == "Trend analysis"
    assert second is None
    assert len(manager.events()) == 1
    assert len(notifications) == 1
    assert "content" not in notifications[0]["data"]
    assert "secret" not in str(notifications)


def test_history_shared_with_graph_is_bounded_to_latest_20(tmp_path, monkeypatch):
    graph = FakeGraph()
    manager = _manager(tmp_path, monkeypatch, graph=graph)
    for index in range(25):
        manager._append_summary({"role": "assistant", "content": f"summary-{index}"})

    asyncio.run(manager._process_batch(object(), 1, mode="automatic"))

    history = graph.calls[0]["history"]
    assert len(history) == 20
    assert history[0]["content"] == "summary-5"
    assert history[-1]["content"] == "summary-24"


def test_transient_failure_retries_without_disabling_or_replaying_backlog(
    tmp_path, monkeypatch
):
    graph = FakeGraph([RuntimeError("temporary"), _graph_result()])
    manager = _manager(tmp_path, monkeypatch, graph=graph)
    sleeps = []

    async def controlled_sleep(delay):
        sleeps.append(delay)
        if len(graph.calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(market_agent.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run(1, immediate=True))

    assert len(graph.calls) == 2
    assert sleeps[0] == 1
    assert manager.status()["desired_enabled"] is True
    assert manager.status()["retry_attempt"] == 0
    assert len(manager.events()) == 1


def test_budget_and_invalid_config_pause_explicitly_without_disabling(
    tmp_path, monkeypatch
):
    budget_graph = FakeGraph()
    budget = _manager(tmp_path / "budget", monkeypatch, graph=budget_graph, daily_limit=1)
    budget._state["daily_usage_count"] = 1

    async def stop_after_pause(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(market_agent.asyncio, "sleep", stop_after_pause)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(budget._run(1, immediate=True))
    assert budget.status()["state"] == "paused_budget"
    assert budget.status()["desired_enabled"] is True
    assert not budget_graph.calls

    config = _manager(tmp_path / "config", monkeypatch, graph=FakeGraph())

    def invalid_config(expected_id):
        raise MarketAgentError("config_unavailable", "invalid configuration")

    monkeypatch.setattr(config, "_resolve_config", invalid_config)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(config._run(1, immediate=True))
    assert config.status()["state"] == "paused_config"
    assert config.status()["paused_reason"] == "config_unavailable"
    assert config.status()["desired_enabled"] is True
