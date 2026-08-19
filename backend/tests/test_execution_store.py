from __future__ import annotations

import json

import pytest

from backend.app.services.execution_store import (
    ExecutionStore,
    ExecutionStoreConflict,
    ExecutionStoreError,
)


def _initialized(tmp_path) -> ExecutionStore:
    store = ExecutionStore(tmp_path)
    store.initialize(
        "testnet",
        "SOLUSDT",
        run_id="run-20260819",
        metadata={"config_version": "v3", "capital_limit_usdt": "100"},
        started_at="2026-08-19T00:00:00Z",
    )
    return store


def _decision(store: ExecutionStore) -> None:
    store.record_decision(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        action="open",
        details={"bar_close_time": 123},
        created_at="2026-08-19T00:05:00Z",
    )


def test_journal_round_trips_after_restart_and_contains_no_position(tmp_path) -> None:
    store = _initialized(tmp_path)
    _decision(store)
    store.record_order_attempt(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        request={"side": "BUY", "quantity": "0.1"},
        attempted_at="2026-08-19T00:05:01Z",
    )
    store.record_order_result(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        status="filled",
        exchange_order_id=42,
        client_order_id="cm-test-order",
        filled_quantity="0.1",
        updated_at="2026-08-19T00:05:02Z",
    )

    restarted = ExecutionStore(tmp_path)
    loaded = restarted.load("testnet", "SOLUSDT")

    assert loaded is not None
    assert loaded["run"]["run_id"] == "run-20260819"
    assert loaded["decisions"]["bar-123:OPEN"]["orders"]["0"]["result"][
        "exchange_order_id"
    ] == 42
    assert "position" not in loaded
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(store.path_for("testnet", "SOLUSDT").read_text())["schema_version"] == 1


def test_decision_and_order_keys_are_idempotent_but_conflicts_fail(tmp_path) -> None:
    store = _initialized(tmp_path)
    _decision(store)
    _decision(store)
    first = store.record_order_attempt(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        request={"side": "BUY", "quantity": "0.1"},
        attempted_at="2026-08-19T00:05:01Z",
    )
    replay = store.record_order_attempt(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        request={"side": "BUY", "quantity": "0.1"},
        attempted_at="later-is-ignored",
    )

    assert replay == first
    with pytest.raises(ExecutionStoreConflict, match="different request"):
        store.record_order_attempt(
            "testnet",
            "SOLUSDT",
            decision_id="bar-123:OPEN",
            ordinal=0,
            request={"side": "BUY", "quantity": "0.2"},
        )
    with pytest.raises(ExecutionStoreConflict, match="different content"):
        store.record_decision(
            "testnet",
            "SOLUSDT",
            decision_id="bar-123:OPEN",
            action="close",
        )


def test_status_summary_counts_results_without_inventing_exchange_state(tmp_path) -> None:
    store = _initialized(tmp_path)
    _decision(store)
    for ordinal, status in enumerate(("submitted", "filled", "rejected", "unknown")):
        store.record_order_attempt(
            "testnet",
            "SOLUSDT",
            decision_id="bar-123:OPEN",
            ordinal=ordinal,
            request={"side": "BUY", "quantity": "0.1", "sequence": ordinal},
        )
        store.record_order_result(
            "testnet",
            "SOLUSDT",
            decision_id="bar-123:OPEN",
            ordinal=ordinal,
            status=status,
        )

    summary = store.status_summary("testnet", "SOLUSDT")

    assert summary is not None
    assert summary["decision_count"] == 1
    assert summary["order_attempt_count"] == 4
    assert summary["submitted_order_count"] == 2
    assert summary["filled_order_count"] == 1
    assert summary["rejected_order_count"] == 1
    assert summary["unknown_order_count"] == 1
    assert "position" not in summary


def test_errors_are_redacted_and_terminal_results_are_immutable(tmp_path) -> None:
    store = _initialized(tmp_path)
    _decision(store)
    store.record_order_attempt(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        request={"side": "BUY"},
    )
    result = store.record_order_result(
        "testnet",
        "SOLUSDT",
        decision_id="bar-123:OPEN",
        ordinal=0,
        status="rejected",
        error=(
            "POST https://example.test/order?apiKey=abcdefghijklmnopqrstuvwxyz123456 "
            "signature=abcdefghijklmnopqrstuvwxyz123456"
        ),
    )

    assert "abcdefghijklmnopqrstuvwxyz123456" not in result["error"]
    assert "[REDACTED]" in result["error"]
    with pytest.raises(ExecutionStoreConflict, match="terminal"):
        store.record_order_result(
            "testnet",
            "SOLUSDT",
            decision_id="bar-123:OPEN",
            ordinal=0,
            status="filled",
        )


def test_schema_identity_sensitive_fields_and_counters_fail_closed(tmp_path) -> None:
    store = _initialized(tmp_path)
    path = store.path_for("testnet", "SOLUSDT")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["counters"]["decision_count"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ExecutionStoreError, match="counters"):
        store.load("testnet", "SOLUSDT")
    with pytest.raises(ValueError, match="sensitive"):
        ExecutionStore(tmp_path / "other").initialize(
            "mainnet",
            "SOLUSDT",
            run_id="run-2",
            metadata={"api_key": "must-not-be-written"},
        )
    with pytest.raises(ValueError, match="network"):
        store.path_for("paper", "SOLUSDT")


def test_initialize_is_bound_to_one_run_and_corruption_is_redacted(tmp_path) -> None:
    store = _initialized(tmp_path)
    same = store.initialize(
        "testnet",
        "SOLUSDT",
        run_id="run-20260819",
        metadata={"config_version": "v3", "capital_limit_usdt": "100"},
    )
    assert same["run"]["run_id"] == "run-20260819"

    with pytest.raises(ExecutionStoreConflict, match="another run"):
        store.initialize("testnet", "SOLUSDT", run_id="run-next")

    store.path_for("testnet", "SOLUSDT").write_text("{broken", encoding="utf-8")
    with pytest.raises(ExecutionStoreError, match="unreadable") as captured:
        store.load("testnet", "SOLUSDT")
    assert str(tmp_path) not in str(captured.value)
