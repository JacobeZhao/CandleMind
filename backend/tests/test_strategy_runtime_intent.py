import json

import pytest

from backend.app.services.strategy_runtime_intent import (
    StrategyRuntimeIntentError,
    StrategyRuntimeIntentStore,
    StrategyRuntimeLeaseConflict,
    StrategyScope,
)


class Clock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def scope():
    return StrategyScope("binance", "testnet", "SOLUSDT")


def test_intent_round_trip_is_atomic_and_preserves_stopped_scope(tmp_path, scope):
    store = StrategyRuntimeIntentStore(tmp_path / "runtime", clock=Clock())

    running = store.request_start(scope, {"strategy": "trend", "risk": {"fraction": 0.2}})
    stopped = store.request_stop(scope, running["config"])

    assert stopped["desired_state"] == "stopped"
    assert stopped["scope"] == scope.as_dict()
    assert stopped["generation"] == 2
    assert store.load() == stopped
    assert json.loads(store.path.read_text(encoding="utf-8")) == stopped
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize(
    "config",
    [{"api_key": "secret"}, {"nested": {"accessToken": "secret"}}, {"risk": float("nan")}],
)
def test_intent_rejects_secrets_and_non_json_values(tmp_path, scope, config):
    store = StrategyRuntimeIntentStore(tmp_path, clock=Clock())
    with pytest.raises(ValueError):
        store.request_start(scope, config)
    assert not store.path.exists()


def test_load_rejects_corrupt_and_unknown_schema(tmp_path):
    store = StrategyRuntimeIntentStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not-json", encoding="utf-8")
    with pytest.raises(StrategyRuntimeIntentError, match="unreadable"):
        store.load()

    store.path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(StrategyRuntimeIntentError, match="schema"):
        store.load()


def test_lease_is_exclusive_for_the_same_scope(tmp_path, scope):
    clock = Clock()
    first = StrategyRuntimeIntentStore(
        tmp_path, clock=clock, hostname="host-a", process_id=101, pid_is_running=lambda _pid: True
    )
    second = StrategyRuntimeIntentStore(
        tmp_path, clock=clock, hostname="host-a", process_id=202, pid_is_running=lambda _pid: True
    )
    lease = first.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=30)

    with pytest.raises(StrategyRuntimeLeaseConflict, match="already has"):
        second.acquire_lease(scope, runtime_id="engine-b", ttl_seconds=30)

    assert first.inspect_lease(scope) == lease


def test_different_scopes_have_independent_leases(tmp_path, scope):
    store = StrategyRuntimeIntentStore(tmp_path, clock=Clock(), hostname="host-a", process_id=101)
    other = StrategyScope("binance", "testnet", "BTCUSDT")

    first = store.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=30)
    second = store.acquire_lease(other, runtime_id="engine-b", ttl_seconds=30)

    assert first.scope == scope
    assert second.scope == other


def test_lease_renewal_and_release_require_matching_identity(tmp_path, scope):
    clock = Clock()
    store = StrategyRuntimeIntentStore(tmp_path, clock=clock, hostname="host-a", process_id=101)
    lease = store.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=30)
    clock.value += 10
    renewed = store.renew_lease(lease, ttl_seconds=60)
    assert renewed.expires_at_epoch == clock.value + 60

    impostor = StrategyRuntimeIntentStore(tmp_path, clock=clock, hostname="host-a", process_id=202)
    foreign = impostor.inspect_lease(scope)
    assert foreign is not None
    with pytest.raises(StrategyRuntimeLeaseConflict, match="process does not own"):
        impostor.release_lease(foreign)

    store.release_lease(renewed)
    assert store.inspect_lease(scope) is None


def test_expired_lease_is_not_automatically_reclaimed(tmp_path, scope):
    clock = Clock()
    dead_owner = StrategyRuntimeIntentStore(
        tmp_path, clock=clock, hostname="host-a", process_id=101, pid_is_running=lambda _pid: False
    )
    lease = dead_owner.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=10)
    clock.value += 11

    audit = dead_owner.audit_lease(scope)
    assert audit["status"] == "stale_confirmed"
    assert audit["reclaimable"] is True
    with pytest.raises(StrategyRuntimeLeaseConflict):
        dead_owner.acquire_lease(scope, runtime_id="engine-b", ttl_seconds=10)
    with pytest.raises(ValueError, match="explicit reason"):
        dead_owner.reclaim_stale_lease(scope, expected_lease_id=lease.lease_id, reason="crash")

    dead_owner.reclaim_stale_lease(
        scope, expected_lease_id=lease.lease_id, reason="audited dead local process"
    )
    replacement = dead_owner.acquire_lease(scope, runtime_id="engine-b", ttl_seconds=10)
    assert replacement.lease_id != lease.lease_id


def test_expired_remote_or_live_owner_lease_cannot_be_reclaimed(tmp_path, scope):
    clock = Clock()
    owner = StrategyRuntimeIntentStore(tmp_path, clock=clock, hostname="host-a", process_id=101)
    lease = owner.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=10)
    clock.value += 11

    remote = StrategyRuntimeIntentStore(
        tmp_path, clock=clock, hostname="host-b", process_id=202, pid_is_running=lambda _pid: False
    )
    assert remote.audit_lease(scope)["status"] == "stale_unverifiable"
    with pytest.raises(StrategyRuntimeLeaseConflict, match="stale_unverifiable"):
        remote.reclaim_stale_lease(
            scope, expected_lease_id=lease.lease_id, reason="remote host is unavailable"
        )

    local = StrategyRuntimeIntentStore(
        tmp_path, clock=clock, hostname="host-a", process_id=202, pid_is_running=lambda _pid: True
    )
    assert local.audit_lease(scope)["status"] == "expired_owner_alive"
    with pytest.raises(StrategyRuntimeLeaseConflict, match="expired_owner_alive"):
        local.reclaim_stale_lease(
            scope, expected_lease_id=lease.lease_id, reason="owner appears to be alive"
        )


def test_restart_audit_is_read_only_and_never_authorizes_orders(tmp_path, scope):
    store = StrategyRuntimeIntentStore(tmp_path, clock=Clock(), hostname="host-a", process_id=101)
    intent = store.request_start(scope, {"strategy": "trend"})

    audit = store.audit_restart()

    assert audit["intent"] == intent
    assert audit["recommended_action"] == "audit_and_reconcile_before_resume"
    assert audit["may_place_orders"] is False
    assert audit["lease"]["status"] == "absent"
    assert not store.lease_root.exists()


def test_tests_use_only_injected_temporary_root(tmp_path, scope):
    store = StrategyRuntimeIntentStore(tmp_path / "isolated", clock=Clock())
    store.request_start(scope, {"strategy": "trend"})
    lease = store.acquire_lease(scope, runtime_id="engine-a", ttl_seconds=10)

    assert store.path.is_relative_to(tmp_path)
    assert store._lease_path(scope).is_relative_to(tmp_path)
    store.release_lease(lease)
