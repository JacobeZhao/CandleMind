import json

import pytest

from backend.app.services.market_agent_state_store import (
    ANALYSIS_INTERVALS,
    MAX_EVENTS,
    MAX_SUMMARIES,
    SCHEMA_VERSION,
    MarketAgentStateError,
    MarketAgentStateStore,
)


def _v1_document(**overrides):
    document = {
        "schema_version": 1,
        "enabled": True,
        "agent_id": "agent-v1",
        "symbol": "SOLUSDT",
        "interval": "15m",
        "config_id": 7,
        "state": "running",
        "last_processed_bar_closed_at": "2026-08-19T10:05:00Z",
        "next_sequence": 2,
        "consecutive_failures": 0,
        "paused_reason": None,
        "daily_usage_date": "2026-08-19",
        "daily_usage_count": 1,
        "started_at": "2026-08-19T10:00:00Z",
        "updated_at": "2026-08-19T10:05:01Z",
        "events": [{"sequence": 1, "answer": "legacy"}],
    }
    document.update(overrides)
    return document


def test_load_migrates_v1_to_v2_and_keeps_one_backup(tmp_path):
    store = MarketAgentStateStore(tmp_path / "agents")
    store.root.mkdir(parents=True)
    legacy = _v1_document()
    store.path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.load()

    assert SCHEMA_VERSION == 2
    assert loaded["schema_version"] == 2
    assert loaded["desired_enabled"] is True
    assert "enabled" not in loaded
    assert loaded["trigger_interval"] == "5m"
    assert tuple(loaded["analysis_intervals"]) == tuple(ANALYSIS_INTERVALS)
    assert loaded["last_scheduled_cutoff"] == "2026-08-19T10:05:00Z"
    backup = store.root / "market_agent.v1.json"
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 2

    store.load()
    assert list(store.root.glob("market_agent.v1*.json")) == [backup]


def test_state_store_bounds_events_and_shared_summaries(tmp_path):
    store = MarketAgentStateStore(tmp_path / "agents")
    payload = {
        "desired_enabled": True,
        "state": "running",
        "symbol": "SOLUSDT",
        "trigger_interval": "5m",
        "analysis_intervals": list(ANALYSIS_INTERVALS),
        "next_sequence": 131,
        "daily_usage_count": 0,
        "events": [{"sequence": value} for value in range(1, 131)],
        "summaries": [{"role": "assistant", "content": str(value)} for value in range(25)],
    }

    path = store.save(payload)
    loaded = store.load()

    assert path == tmp_path / "agents" / "market_agent.json"
    assert len(loaded["events"]) == MAX_EVENTS
    assert loaded["events"][0]["sequence"] == 31
    assert loaded["events"][-1]["sequence"] == 130
    assert len(loaded["summaries"]) == MAX_SUMMARIES == 20
    assert loaded["summaries"][0]["content"] == "5"
    assert not list(path.parent.glob("*.tmp"))


def test_state_store_rejects_incompatible_or_invalid_documents(tmp_path):
    store = MarketAgentStateStore(tmp_path / "agents")
    store.root.mkdir(parents=True)
    store.path.write_text(json.dumps({"schema_version": 99, "events": []}), encoding="utf-8")

    with pytest.raises(MarketAgentStateError, match="schema"):
        store.load()

    store.path.write_text("not-json", encoding="utf-8")
    with pytest.raises(MarketAgentStateError, match="unreadable"):
        store.load()


def test_state_store_normalizes_fixed_schedule_and_rejects_non_finite_values(tmp_path):
    store = MarketAgentStateStore(tmp_path / "agents")
    store.save(
        {
            "desired_enabled": True,
            "state": "running",
            "symbol": "SOLUSDT",
            "trigger_interval": "15m",
            "analysis_intervals": ["1h"],
            "next_sequence": 1,
            "daily_usage_count": 0,
            "events": [],
            "summaries": [],
        }
    )
    loaded = store.load()
    assert loaded["trigger_interval"] == "5m"
    assert tuple(loaded["analysis_intervals"]) == tuple(ANALYSIS_INTERVALS)
    with pytest.raises(MarketAgentStateError, match="serializable"):
        store.save({"events": [], "summaries": [{"content": float("nan")}]})
