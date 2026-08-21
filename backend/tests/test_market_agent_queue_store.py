from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from backend.app.services.market_agent_contracts import (
    JobLane,
    JobState,
    MarketAgentLedgerConflict,
    MarketAgentLeaseError,
)
from backend.app.services.market_agent_queue_store import MarketAgentQueueStore


def _store(tmp_path) -> MarketAgentQueueStore:
    return MarketAgentQueueStore(tmp_path / "agents" / "harness.sqlite3")


def test_initialization_is_atomic_idempotent_and_enables_wal(tmp_path):
    store = _store(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.initialize(), range(16)))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "1"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"jobs", "inbox_messages", "outbox_events"} <= tables


def test_market_dedupe_is_scoped_and_merges_reasons_and_priority(tmp_path):
    store = _store(tmp_path)
    first = store.enqueue_market_job(
        "mainnet",
        "SOLUSDT",
        "5m:100:399999",
        payload={"cutoff_ms": 399_999},
        reasons=["candle_closed"],
        priority=1,
        now_ms=100,
    )
    duplicate = store.enqueue_market_job(
        "mainnet",
        "SOLUSDT",
        "5m:100:399999",
        payload={"cutoff_ms": 399_999},
        reasons=["sar_reversal", "candle_closed"],
        priority=10,
        now_ms=101,
    )
    other_scope = store.enqueue_market_job(
        "testnet",
        "SOLUSDT",
        "5m:100:399999",
        payload={"cutoff_ms": 399_999},
        now_ms=102,
    )

    assert duplicate.id == first.id
    assert duplicate.reasons == ("candle_closed", "sar_reversal")
    assert duplicate.priority == 10
    assert other_scope.id != first.id
    with pytest.raises(MarketAgentLedgerConflict):
        store.enqueue_market_job(
            "mainnet",
            "SOLUSDT",
            "5m:100:399999",
            payload={"cutoff_ms": 999_999},
        )


def test_inbox_client_message_id_is_idempotent_within_scope(tmp_path):
    store = _store(tmp_path)
    first = store.enqueue_inbox_message(
        "mainnet", "SOLUSDT", "browser-42", "Analyze the current trend", now_ms=10
    )
    duplicate = store.enqueue_inbox_message(
        "mainnet", "SOLUSDT", "browser-42", "Analyze the current trend", now_ms=11
    )
    another_symbol = store.enqueue_inbox_message(
        "mainnet", "BTCUSDT", "browser-42", "Analyze the current trend", now_ms=12
    )

    assert first.lane is JobLane.INBOX
    assert duplicate.id == first.id
    assert another_symbol.id != first.id
    with pytest.raises(MarketAgentLedgerConflict):
        store.enqueue_inbox_message(
            "mainnet", "SOLUSDT", "browser-42", "Different request"
        )


def test_claim_is_transactional_and_honors_scope_lane_and_priority(tmp_path):
    store = _store(tmp_path)
    low = store.enqueue_market_job(
        "mainnet", "SOLUSDT", "low", priority=1, now_ms=10
    )
    high = store.enqueue_inbox_message(
        "mainnet", "SOLUSDT", "high", "question", priority=9, now_ms=11
    )
    store.enqueue_market_job("mainnet", "BTCUSDT", "other", priority=99, now_ms=12)

    claimed = store.claim_next(
        "worker-a",
        lease_ms=1_000,
        network="mainnet",
        symbol="SOLUSDT",
        now_ms=20,
    )
    market_only = store.claim_next(
        "worker-b", lease_ms=1_000, lanes=[JobLane.MARKET], now_ms=20
    )

    assert claimed.id == high.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempts == 1
    assert market_only.id != low.id  # BTC has the higher priority in the market lane.

    only_store = _store(tmp_path / "concurrent")
    job = only_store.enqueue_market_job("mainnet", "SOLUSDT", "single", now_ms=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda worker: only_store.claim_next(worker, lease_ms=1_000, now_ms=2),
                ("one", "two"),
            )
        )
    assert [claim.id for claim in claims if claim is not None] == [job.id]


def test_expired_lease_is_recovered_and_wrong_owner_cannot_transition(tmp_path):
    store = _store(tmp_path)
    job = store.enqueue_market_job("mainnet", "SOLUSDT", "lease", now_ms=1)
    store.claim_next("worker-a", lease_ms=10, now_ms=2)

    with pytest.raises(MarketAgentLeaseError):
        store.retry_job(
            job.id,
            "worker-b",
            available_at_ms=20,
            error_code="temporary",
            now_ms=3,
        )
    with pytest.raises(MarketAgentLeaseError, match="expired"):
        store.fail_job(job.id, "worker-a", error_code="late", now_ms=12)

    restarted = MarketAgentQueueStore(store.path)
    assert restarted.recover_expired_leases(now_ms=12) == 1
    recovered = restarted.get_job(job.id)
    assert recovered.state is JobState.RETRY_WAIT
    assert recovered.error_code == "lease_expired"
    reclaimed = restarted.claim_next("worker-b", lease_ms=10, now_ms=12)
    assert reclaimed.id == job.id
    assert reclaimed.attempts == 2


def test_lease_can_be_renewed_only_by_the_current_owner(tmp_path):
    store = _store(tmp_path)
    job = store.enqueue_market_job("mainnet", "SOLUSDT", "renew", now_ms=1)
    store.claim_next("worker-a", lease_ms=10, now_ms=2)

    renewed = store.renew_lease(job.id, "worker-a", lease_ms=20, now_ms=5)
    assert renewed.lease_expires_at_ms == 25
    with pytest.raises(MarketAgentLeaseError):
        store.renew_lease(job.id, "worker-b", lease_ms=20, now_ms=6)


def test_retry_failed_and_superseded_transitions(tmp_path):
    store = _store(tmp_path)
    retry = store.enqueue_market_job("mainnet", "SOLUSDT", "retry", now_ms=1)
    store.claim_next("worker", lease_ms=100, now_ms=2)
    waiting = store.retry_job(
        retry.id,
        "worker",
        available_at_ms=50,
        error_code="provider_timeout",
        now_ms=3,
    )
    assert waiting.state is JobState.RETRY_WAIT
    assert store.claim_next("worker", lease_ms=100, now_ms=49) is None
    store.claim_next("worker", lease_ms=100, now_ms=50)
    failed = store.fail_job(
        retry.id, "worker", error_code="provider_auth_failed", now_ms=51
    )
    assert failed.state is JobState.FAILED
    assert failed.completed_at_ms == 51

    stale = store.enqueue_market_job("mainnet", "SOLUSDT", "stale", now_ms=60)
    superseded = store.supersede_job(stale.id, reason="backlog_coalesced", now_ms=61)
    assert superseded.state is JobState.SUPERSEDED
    assert store.supersede_job(stale.id, now_ms=62).state is JobState.SUPERSEDED


def test_completion_and_outbox_publication_are_idempotent(tmp_path):
    store = _store(tmp_path)
    job = store.enqueue_market_job(
        "mainnet",
        "SOLUSDT",
        "closed:399999",
        reasons=["candle_closed", "large_candle"],
        now_ms=1,
    )
    store.claim_next("worker", lease_ms=1_000, now_ms=2)
    event = store.complete_job(
        job.id,
        "worker",
        result={"regime": "trend_up"},
        event_type="analysis",
        role="assistant",
        content="Trend remains constructive.",
        structured={"regime": "trend_up", "confidence": 0.7},
        now_ms=3,
    )
    duplicate = store.complete_job(
        job.id,
        "worker",
        result={"ignored": True},
        event_type="analysis",
        role="assistant",
        content="This duplicate must not replace the committed event.",
        now_ms=4,
    )

    assert duplicate == event
    assert event.sequence == 1
    assert event.reasons == ("candle_closed", "large_candle")
    assert store.get_job(job.id).state is JobState.COMPLETED
    assert store.events(unpublished_only=True) == [event]

    published = store.mark_event_published(event.sequence, now_ms=5)
    published_again = store.mark_event_published(event.sequence, now_ms=9)
    assert published.published_at_ms == 5
    assert published_again.published_at_ms == 5
    assert store.get_job(job.id).state is JobState.PUBLISHED
    assert store.events(unpublished_only=True) == []


def test_event_sequences_and_status_summary_are_scope_isolated(tmp_path):
    store = _store(tmp_path)
    first = store.enqueue_market_job("mainnet", "SOLUSDT", "one", now_ms=1)
    second = store.enqueue_inbox_message(
        "mainnet", "SOLUSDT", "two", "question", now_ms=2
    )
    store.enqueue_market_job("testnet", "SOLUSDT", "three", now_ms=3)

    for worker, job in (("a", first), ("b", second)):
        store.claim_next(
            worker,
            lease_ms=100,
            lanes=[job.lane],
            network="mainnet",
            symbol="SOLUSDT",
            now_ms=10,
        )
        store.complete_job(
            job.id,
            worker,
            result={"ok": True},
            event_type="analysis",
            role="assistant",
            content="done",
            now_ms=11,
        )

    events = store.events(after_sequence=1)
    assert [event.sequence for event in events] == [2]
    summary = store.status_summary("mainnet", "SOLUSDT")
    assert summary["states"]["completed"] == 2
    assert summary["lanes"] == {"market": 0, "inbox": 0}
    assert summary["queue_depth"] == 0
    assert summary["latest_sequence"] == 2
    assert summary["unpublished_events"] == 2
    assert store.status_summary("testnet", "SOLUSDT")["queue_depth"] == 1
