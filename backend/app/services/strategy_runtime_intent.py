"""Persistent strategy intent and conservative single-runtime leasing.

This module stores operator intent only. Restart auditing is deliberately
read-only and never starts an engine or submits an order.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
from threading import RLock
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

from backend.app.runtime_paths import RUNTIME_DATA_DIR


SCHEMA_VERSION = 1
DESIRED_STATES = {"running", "stopped"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|signature|passphrase|authorization|credential|token)",
    re.IGNORECASE,
)
_LOCK = RLock()


class StrategyRuntimeIntentError(RuntimeError):
    """Raised when runtime intent or lease state cannot be trusted."""


class StrategyRuntimeLeaseConflict(StrategyRuntimeIntentError):
    """Raised when another runtime owns or may own the trading scope."""


@dataclass(frozen=True)
class StrategyScope:
    provider: str
    network: str
    symbol: str

    def __post_init__(self) -> None:
        _validate_identifier(self.provider, "provider")
        _validate_identifier(self.network, "network")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("invalid strategy symbol")
        object.__setattr__(self, "provider", self.provider.lower())
        object.__setattr__(self, "network", self.network.lower())

    @property
    def key(self) -> str:
        return f"{self.provider.lower()}_{self.network.lower()}_{self.symbol}"

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "network": self.network, "symbol": self.symbol}


@dataclass(frozen=True)
class TradingLease:
    lease_id: str
    runtime_id: str
    scope: StrategyScope
    owner_pid: int
    owner_host: str
    acquired_at: str
    renewed_at: str
    expires_at_epoch: float

    @classmethod
    def from_document(cls, payload: dict[str, Any]) -> "TradingLease":
        scope = StrategyScope(**payload["scope"])
        return cls(
            lease_id=payload["lease_id"],
            runtime_id=payload["runtime_id"],
            scope=scope,
            owner_pid=payload["owner_pid"],
            owner_host=payload["owner_host"],
            acquired_at=payload["acquired_at"],
            renewed_at=payload["renewed_at"],
            expires_at_epoch=payload["expires_at_epoch"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "lease_id": self.lease_id,
            "runtime_id": self.runtime_id,
            "scope": self.scope.as_dict(),
            "owner_pid": self.owner_pid,
            "owner_host": self.owner_host,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at_epoch": self.expires_at_epoch,
        }


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid strategy {label}")


def _json_safe(value: Any, *, label: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be strings")
            if _SENSITIVE_KEY.search(key):
                raise ValueError(f"{label} contains a sensitive field")
            result[key] = _json_safe(item, label=label)
        return result
    if isinstance(value, list):
        return [_json_safe(item, label=label) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{label} is not JSON-safe")


def _utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _pid_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class StrategyRuntimeIntentStore:
    """Store desired strategy scope and coordinate its exclusive trading lease."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        hostname: str | None = None,
        process_id: int | None = None,
        pid_is_running: Callable[[int], bool] = _pid_is_running,
    ) -> None:
        self.root = (root or RUNTIME_DATA_DIR / "strategies").resolve()
        self.path = self.root / "runtime_intent.json"
        self.lease_root = self.root / "leases"
        self._clock = clock
        self._hostname = hostname or socket.gethostname()
        self._process_id = process_id if process_id is not None else os.getpid()
        self._pid_is_running = pid_is_running

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        payload = self._read_json(self.path, "strategy runtime intent")
        return self._validate_intent(payload)

    def set_desired_state(
        self,
        desired_state: str,
        scope: StrategyScope,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist operator intent without acquiring a lease or running strategy code."""

        if desired_state not in DESIRED_STATES:
            raise ValueError("invalid desired strategy state")
        safe_config = _json_safe(config, label="strategy config")
        if not isinstance(safe_config, dict):
            raise ValueError("strategy config must be an object")
        with _LOCK:
            now = self._clock()
            previous = self.load()
            generation = 1 if previous is None else previous["generation"] + 1
            document = {
                "schema_version": SCHEMA_VERSION,
                "desired_state": desired_state,
                "scope": scope.as_dict(),
                "config": safe_config,
                "generation": generation,
                "updated_at": _utc_timestamp(now),
            }
            self._atomic_write(self.path, document, exclusive=False)
        return deepcopy(document)

    def request_start(self, scope: StrategyScope, config: dict[str, Any]) -> dict[str, Any]:
        return self.set_desired_state("running", scope, config)

    def request_stop(self, scope: StrategyScope, config: dict[str, Any]) -> dict[str, Any]:
        return self.set_desired_state("stopped", scope, config)

    def acquire_lease(
        self, scope: StrategyScope, *, runtime_id: str, ttl_seconds: float
    ) -> TradingLease:
        """Acquire a scope exclusively; existing leases are never auto-reclaimed."""

        _validate_identifier(runtime_id, "runtime_id")
        ttl = self._validate_ttl(ttl_seconds)
        now = self._clock()
        stamp = _utc_timestamp(now)
        lease = TradingLease(
            lease_id=uuid4().hex,
            runtime_id=runtime_id,
            scope=scope,
            owner_pid=self._process_id,
            owner_host=self._hostname,
            acquired_at=stamp,
            renewed_at=stamp,
            expires_at_epoch=now + ttl,
        )
        path = self._lease_path(scope)
        with _LOCK:
            try:
                self._atomic_write(path, lease.as_dict(), exclusive=True)
            except FileExistsError as exc:
                audit = self.audit_lease(scope)
                raise StrategyRuntimeLeaseConflict(
                    f"trading scope already has a {audit['status']} lease; "
                    "inspect it and explicitly reclaim only a proven stale lease"
                ) from exc
        return lease

    def inspect_lease(self, scope: StrategyScope) -> TradingLease | None:
        path = self._lease_path(scope)
        if not path.exists():
            return None
        return self._validate_lease(self._read_json(path, "trading lease"), scope)

    def audit_lease(self, scope: StrategyScope) -> dict[str, Any]:
        """Classify a lease without changing or acquiring it."""

        lease = self.inspect_lease(scope)
        if lease is None:
            return {"status": "absent", "lease": None, "reclaimable": False}
        expired = lease.expires_at_epoch <= self._clock()
        if not expired:
            status = "active"
            reclaimable = False
        elif lease.owner_host != self._hostname:
            status = "stale_unverifiable"
            reclaimable = False
        elif self._pid_is_running(lease.owner_pid):
            status = "expired_owner_alive"
            reclaimable = False
        else:
            status = "stale_confirmed"
            reclaimable = True
        return {"status": status, "lease": lease, "reclaimable": reclaimable}

    def reclaim_stale_lease(
        self, scope: StrategyScope, *, expected_lease_id: str, reason: str
    ) -> None:
        """Remove only an audited, expired lease whose local owner is confirmed dead."""

        _validate_identifier(expected_lease_id, "lease_id")
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            raise ValueError("stale lease recovery requires an explicit reason")
        with _LOCK:
            audit = self.audit_lease(scope)
            lease = audit["lease"]
            if lease is None or lease.lease_id != expected_lease_id:
                raise StrategyRuntimeLeaseConflict("stale lease identity changed")
            if not audit["reclaimable"]:
                raise StrategyRuntimeLeaseConflict(
                    f"lease cannot be reclaimed because its status is {audit['status']}"
                )
            self._lease_path(scope).unlink()

    def renew_lease(self, lease: TradingLease, *, ttl_seconds: float) -> TradingLease:
        ttl = self._validate_ttl(ttl_seconds)
        with _LOCK:
            current = self._require_owned_lease(lease)
            now = self._clock()
            if current.expires_at_epoch <= now:
                raise StrategyRuntimeLeaseConflict("expired trading lease cannot be renewed")
            renewed = TradingLease(
                lease_id=current.lease_id,
                runtime_id=current.runtime_id,
                scope=current.scope,
                owner_pid=current.owner_pid,
                owner_host=current.owner_host,
                acquired_at=current.acquired_at,
                renewed_at=_utc_timestamp(now),
                expires_at_epoch=now + ttl,
            )
            self._atomic_write(self._lease_path(lease.scope), renewed.as_dict(), exclusive=False)
            return renewed

    def release_lease(self, lease: TradingLease) -> None:
        with _LOCK:
            self._require_owned_lease(lease)
            self._lease_path(lease.scope).unlink()

    def audit_restart(self) -> dict[str, Any]:
        """Return recovery requirements; this method has no execution side effects."""

        intent = self.load()
        if intent is None:
            return {
                "intent": None,
                "lease": {"status": "not_applicable", "reclaimable": False},
                "recommended_action": "remain_stopped",
                "may_place_orders": False,
            }
        scope = StrategyScope(**intent["scope"])
        lease_audit = self.audit_lease(scope)
        action = (
            "audit_and_reconcile_before_resume"
            if intent["desired_state"] == "running"
            else "remain_stopped"
        )
        return {
            "intent": intent,
            "lease": lease_audit,
            "recommended_action": action,
            "may_place_orders": False,
        }

    def _require_owned_lease(self, expected: TradingLease) -> TradingLease:
        current = self.inspect_lease(expected.scope)
        if current is None or current.lease_id != expected.lease_id:
            raise StrategyRuntimeLeaseConflict("runtime does not own the trading lease")
        if current.runtime_id != expected.runtime_id:
            raise StrategyRuntimeLeaseConflict("trading lease runtime identity changed")
        if current.owner_host != self._hostname or current.owner_pid != self._process_id:
            raise StrategyRuntimeLeaseConflict("runtime process does not own the trading lease")
        return current

    def _lease_path(self, scope: StrategyScope) -> Path:
        return self.lease_root / f"{scope.key}.lease.json"

    @staticmethod
    def _validate_ttl(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("lease TTL must be a positive finite number")
        ttl = float(value)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("lease TTL must be a positive finite number")
        return ttl

    def _validate_intent(self, payload: Any) -> dict[str, Any]:
        required = {
            "schema_version", "desired_state", "scope", "config", "generation", "updated_at"
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise StrategyRuntimeIntentError("strategy runtime intent schema is incompatible")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise StrategyRuntimeIntentError("strategy runtime intent schema is incompatible")
        if payload["desired_state"] not in DESIRED_STATES:
            raise StrategyRuntimeIntentError("strategy runtime desired state is invalid")
        if not isinstance(payload["scope"], dict) or set(payload["scope"]) != {
            "provider", "network", "symbol"
        }:
            raise StrategyRuntimeIntentError("strategy runtime scope is invalid")
        try:
            StrategyScope(**payload["scope"])
            config = _json_safe(payload["config"], label="strategy config")
        except ValueError as exc:
            raise StrategyRuntimeIntentError(str(exc)) from exc
        if not isinstance(config, dict):
            raise StrategyRuntimeIntentError("strategy config must be an object")
        generation = payload["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise StrategyRuntimeIntentError("strategy runtime generation is invalid")
        if not self._valid_timestamp(payload["updated_at"]):
            raise StrategyRuntimeIntentError("strategy runtime timestamp is invalid")
        return deepcopy(payload)

    def _validate_lease(self, payload: Any, scope: StrategyScope) -> TradingLease:
        required = {
            "schema_version", "lease_id", "runtime_id", "scope", "owner_pid", "owner_host",
            "acquired_at", "renewed_at", "expires_at_epoch"
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise StrategyRuntimeIntentError("trading lease schema is incompatible")
        try:
            lease = TradingLease.from_document(payload)
            _validate_identifier(lease.lease_id, "lease_id")
            _validate_identifier(lease.runtime_id, "runtime_id")
        except (KeyError, TypeError, ValueError) as exc:
            raise StrategyRuntimeIntentError("trading lease schema is incompatible") from exc
        if payload["schema_version"] != SCHEMA_VERSION or lease.scope != scope:
            raise StrategyRuntimeIntentError("trading lease scope is incompatible")
        if (
            isinstance(lease.owner_pid, bool)
            or not isinstance(lease.owner_pid, int)
            or lease.owner_pid <= 0
            or not isinstance(lease.owner_host, str)
            or not lease.owner_host
            or not self._valid_timestamp(lease.acquired_at)
            or not self._valid_timestamp(lease.renewed_at)
            or not isinstance(lease.expires_at_epoch, (int, float))
            or isinstance(lease.expires_at_epoch, bool)
            or not math.isfinite(lease.expires_at_epoch)
        ):
            raise StrategyRuntimeIntentError("trading lease schema is incompatible")
        return lease

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str) or not value.endswith("Z"):
            return False
        try:
            datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            return False
        return True

    @staticmethod
    def _read_json(path: Path, label: str) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StrategyRuntimeIntentError(f"{label} is unreadable") from exc

    def _atomic_write(self, path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive:
                try:
                    target = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    raise
                else:
                    os.close(target)
                    try:
                        os.replace(temporary_name, path)
                        temporary_name = None
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
            else:
                os.replace(temporary_name, path)
                temporary_name = None
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


__all__ = [
    "StrategyRuntimeIntentError",
    "StrategyRuntimeIntentStore",
    "StrategyRuntimeLeaseConflict",
    "StrategyScope",
    "TradingLease",
]
