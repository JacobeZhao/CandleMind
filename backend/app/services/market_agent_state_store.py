"""Versioned, atomic persistence for the continuous market analysis agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from threading import Lock
import tempfile
from typing import Any

from backend.app.runtime_paths import RUNTIME_DATA_DIR


SCHEMA_VERSION = 2
MAX_EVENTS = 100
MAX_SUMMARIES = 20
TRIGGER_INTERVAL = "5m"
ANALYSIS_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,16}USDT$")
_STATES = {
    "stopped",
    "running",
    "waiting_market",
    "retry_wait",
    "paused_budget",
    "paused_config",
}
_PROCESS_LOCK_GUARD = Lock()
_PROCESS_LOCK_REFERENCES: dict[Path, int] = {}


class MarketAgentStateError(RuntimeError):
    pass


def _default_document() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "desired_enabled": False,
        "agent_id": None,
        "symbol": None,
        "config_id": None,
        "state": "stopped",
        "trigger_interval": TRIGGER_INTERVAL,
        "analysis_intervals": list(ANALYSIS_INTERVALS),
        "last_scheduled_cutoff": None,
        "last_committed_batch_id": None,
        "active_batch_id": None,
        "active_thread_id": None,
        "retry_attempt": 0,
        "retry_not_before": None,
        "paused_reason": None,
        "daily_usage_date": None,
        "daily_usage_count": 0,
        "next_sequence": 1,
        "started_at": None,
        "updated_at": None,
        "events": [],
        "summaries": [],
    }


def _legacy_event(event: dict[str, Any]) -> dict[str, Any]:
    if "type" in event and "content" in event:
        return dict(event)
    migrated = dict(event)
    migrated["type"] = "analysis"
    migrated["role"] = "assistant"
    migrated["content"] = str(event.get("answer", ""))
    if "batch_id" not in migrated:
        symbol = event.get("symbol", "UNKNOWN")
        cutoff = event.get("bar_closed_at", event.get("created_at", "unknown"))
        migrated["batch_id"] = f"{symbol}:{cutoff}"
    return migrated


class MarketAgentStateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or RUNTIME_DATA_DIR / "agents").resolve()
        self.path = self.root / "market_agent.json"
        self.v1_backup_path = self.root / "market_agent.v1.json"
        self.worker_lock_path = self.root / "market_agent.worker.lock"
        self._owns_worker_lock = False

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw_text = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MarketAgentStateError("market agent state is unreadable") from exc
        if not isinstance(payload, dict):
            raise MarketAgentStateError("market agent state schema is incompatible")
        version = payload.get("schema_version")
        if version == 1:
            self._backup_v1(raw_text)
            migrated = self._normalize(payload, migrating_v1=True)
            self.save(migrated)
            return migrated
        if version != SCHEMA_VERSION:
            raise MarketAgentStateError("market agent state schema is incompatible")
        return self._normalize(payload)

    def _backup_v1(self, raw_text: str) -> None:
        if self.v1_backup_path.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.v1_backup_path, raw_text)

    def _normalize(
        self, payload: dict[str, Any], *, migrating_v1: bool = False
    ) -> dict[str, Any]:
        document = _default_document()
        document.update(payload)
        if migrating_v1 or "desired_enabled" not in payload:
            document["desired_enabled"] = payload.get("enabled", False)
            document["last_scheduled_cutoff"] = payload.get(
                "last_processed_bar_closed_at"
            )
            document["retry_attempt"] = payload.get("consecutive_failures", 0)
            document["active_batch_id"] = None
            document["active_thread_id"] = None
            document["retry_not_before"] = None
            if document.get("state") in {"starting", "paused_error"}:
                document["state"] = "retry_wait"
        document["schema_version"] = SCHEMA_VERSION
        document["trigger_interval"] = TRIGGER_INTERVAL
        document["analysis_intervals"] = list(ANALYSIS_INTERVALS)
        document["events"] = [
            _legacy_event(event) for event in document.get("events", [])
        ][-MAX_EVENTS:]
        if migrating_v1 and not document.get("summaries"):
            document["summaries"] = [
                {
                    "role": "assistant",
                    "content": event.get("content", "")[:600],
                    "batch_id": event.get("batch_id"),
                }
                for event in document["events"]
                if event.get("content")
            ][-MAX_SUMMARIES:]
        else:
            document["summaries"] = list(document.get("summaries", []))[-MAX_SUMMARIES:]

        legacy_interval = payload.get("interval")
        if legacy_interval is not None and legacy_interval not in ANALYSIS_INTERVALS:
            raise MarketAgentStateError("market agent interval is invalid")
        if not isinstance(document.get("desired_enabled"), bool):
            raise MarketAgentStateError("market agent desired enabled flag is invalid")
        if document.get("state") not in _STATES:
            raise MarketAgentStateError("market agent lifecycle state is invalid")
        symbol = document.get("symbol")
        if symbol is not None and (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)):
            raise MarketAgentStateError("market agent symbol is invalid")
        if document["desired_enabled"] and symbol is None:
            raise MarketAgentStateError("market agent context is incomplete")
        if not isinstance(document.get("next_sequence"), int) or document["next_sequence"] < 1:
            raise MarketAgentStateError("market agent sequence is invalid")
        for field in ("daily_usage_count", "retry_attempt"):
            if not isinstance(document.get(field), int) or document[field] < 0:
                raise MarketAgentStateError(f"market agent {field} is invalid")
        if not isinstance(document.get("events"), list) or not all(
            isinstance(event, dict) and isinstance(event.get("sequence"), int)
            for event in document["events"]
        ):
            raise MarketAgentStateError("market agent event history is invalid")
        if not isinstance(document.get("summaries"), list) or not all(
            isinstance(summary, dict) for summary in document["summaries"]
        ):
            raise MarketAgentStateError("market agent summaries are invalid")

        allowed = set(_default_document())
        return {key: document.get(key) for key in allowed}

    def save(self, payload: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        try:
            json.dumps(payload, ensure_ascii=True, allow_nan=False)
            document = self._normalize(dict(payload))
            encoded = json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MarketAgentStateError("market agent state is not serializable") from exc
        self._atomic_write(self.path, encoded)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self.root
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise MarketAgentStateError("could not atomically save market agent state") from exc
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def acquire_worker_lock(self) -> None:
        if self._owns_worker_lock:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK_GUARD:
            references = _PROCESS_LOCK_REFERENCES.get(self.worker_lock_path, 0)
            if references:
                _PROCESS_LOCK_REFERENCES[self.worker_lock_path] = references + 1
                self._owns_worker_lock = True
                return
            for _ in range(2):
                try:
                    descriptor = os.open(
                        self.worker_lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                        handle.write(str(os.getpid()))
                        handle.flush()
                        os.fsync(handle.fileno())
                    _PROCESS_LOCK_REFERENCES[self.worker_lock_path] = 1
                    self._owns_worker_lock = True
                    return
                except FileExistsError:
                    try:
                        pid = int(self.worker_lock_path.read_text(encoding="ascii").strip())
                    except (OSError, ValueError):
                        pid = -1
                    if pid > 0 and _pid_is_running(pid):
                        raise MarketAgentStateError(
                            "market agent requires a single Uvicorn worker"
                        )
                    self.worker_lock_path.unlink(missing_ok=True)
            raise MarketAgentStateError("market agent worker lock is unavailable")

    def release_worker_lock(self) -> None:
        if not self._owns_worker_lock:
            return
        with _PROCESS_LOCK_GUARD:
            references = _PROCESS_LOCK_REFERENCES.get(self.worker_lock_path, 0)
            if references > 1:
                _PROCESS_LOCK_REFERENCES[self.worker_lock_path] = references - 1
            else:
                _PROCESS_LOCK_REFERENCES.pop(self.worker_lock_path, None)
                try:
                    owner = int(self.worker_lock_path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    owner = None
                if owner == os.getpid():
                    self.worker_lock_path.unlink(missing_ok=True)
            self._owns_worker_lock = False


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
