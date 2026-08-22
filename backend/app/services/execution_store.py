"""Atomic execution journal for exchange-backed strategy runs.

The journal records decisions and exchange responses for recovery and audit. It
does not model, derive, or claim the current exchange position.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from threading import RLock
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.app.runtime_paths import RUNTIME_DATA_DIR


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
NETWORKS = {"testnet", "mainnet"}
DECISION_STATUSES = {
    "prepared",
    "executing",
    "committed",
    "failed",
    "recovery_required",
}
RESOLVED_DECISION_STATUSES = {"committed", "failed"}
_DECISION_TRANSITIONS = {
    "prepared": {"executing", "committed", "failed", "recovery_required"},
    "executing": {"committed", "failed", "recovery_required"},
    "committed": set(),
    "failed": set(),
    "recovery_required": set(),
}
RESULT_STATUSES = {
    "pending",
    "submitted",
    "partially_filled",
    "filled",
    "rejected",
    "cancelled",
    "unknown",
}
TERMINAL_STATUSES = {"filled", "rejected", "cancelled"}
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|signature|passphrase|authorization|credential|token)",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|signature|token|authorization)=([^&\s]+)"
)
_LONG_SECRET = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")


class ExecutionStoreError(RuntimeError):
    """Raised when an execution journal cannot be safely read or written."""


class ExecutionStoreConflict(ExecutionStoreError):
    """Raised when an idempotency key is reused with different content."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_identity(network: str, symbol: str) -> None:
    if network not in NETWORKS:
        raise ValueError("invalid execution network")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("invalid execution symbol")


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}")


def _safe_payload(value: Any, *, label: str) -> Any:
    """Copy JSON data while refusing credential-shaped keys and invalid numbers."""

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be strings")
            if _SENSITIVE_KEY.search(key):
                raise ValueError(f"{label} contains a sensitive field")
            output[key] = _safe_payload(item, label=label)
        return output
    if isinstance(value, list):
        return [_safe_payload(item, label=label) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} is not JSON-safe") from exc
        return value
    raise ValueError(f"{label} is not JSON-safe")


def redact_error(error: object | None) -> str | None:
    """Return a bounded error suitable for persistence and status responses."""

    if error is None:
        return None
    text = str(error).replace("\r", " ").replace("\n", " ")
    text = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    for token in re.findall(r"https?://[^\s]+", text):
        try:
            parts = urlsplit(token)
            clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            text = text.replace(token, clean)
        except ValueError:
            text = text.replace(token, "[REDACTED_URL]")
    text = _LONG_SECRET.sub("[REDACTED]", text)
    return text[:500]


class ExecutionStore:
    """Persist one execution journal per network and symbol."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or RUNTIME_DATA_DIR / "strategies").resolve()
        self._lock = RLock()

    def path_for(self, network: str, symbol: str) -> Path:
        _validate_identity(network, symbol)
        return self.root / f"execution_{network}_{symbol}.json"

    def initialize(
        self,
        network: str,
        symbol: str,
        *,
        run_id: str,
        metadata: dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a bound journal, or return the existing matching run."""

        _validate_identity(network, symbol)
        _validate_identifier(run_id, "run_id")
        safe_metadata = _safe_payload(metadata or {}, label="run metadata")
        with self._lock:
            existing = self.load(network, symbol)
            if existing is not None:
                expected = {"run_id": run_id, "metadata": safe_metadata}
                actual = {
                    "run_id": existing["run"]["run_id"],
                    "metadata": existing["run"]["metadata"],
                }
                if actual != expected:
                    raise ExecutionStoreConflict(
                        "execution journal is already bound to another run"
                    )
                return existing
            now = _utc_now()
            document = {
                "schema_version": SCHEMA_VERSION,
                "network": network,
                "symbol": symbol,
                "run": {
                    "run_id": run_id,
                    "started_at": started_at or now,
                    "metadata": safe_metadata,
                },
                "decisions": {},
                "counters": self._empty_counters(),
                "updated_at": now,
            }
            self._save(document)
            return deepcopy(document)

    def load(self, network: str, symbol: str) -> dict[str, Any] | None:
        path = self.path_for(network, symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExecutionStoreError("execution journal is unreadable") from exc
        if isinstance(payload, dict) and payload.get("schema_version") == LEGACY_SCHEMA_VERSION:
            with self._lock:
                return self._migrate_v1(payload, network=network, symbol=symbol, path=path)
        return self._validate_document(payload, network=network, symbol=symbol)

    def archive(self, network: str, symbol: str) -> Path | None:
        """Atomically move a reconciled, inactive journal into the archive tree."""

        _validate_identity(network, symbol)
        with self._lock:
            document = self.load(network, symbol)
            if document is None:
                return None
            for decision in document["decisions"].values():
                if decision["status"] not in RESOLVED_DECISION_STATUSES:
                    raise ExecutionStoreConflict(
                        "execution journal has unresolved decisions and cannot be archived"
                    )
                for order in decision["orders"].values():
                    if order["result"]["status"] not in TERMINAL_STATUSES:
                        raise ExecutionStoreConflict(
                            "execution journal has non-terminal orders and cannot be archived"
                        )
            source = self.path_for(network, symbol)
            archive_dir = self.root / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = archive_dir / f"{source.stem}_{stamp}{source.suffix}"
            os.replace(source, target)
            return target

    def record_decision(
        self,
        network: str,
        symbol: str,
        *,
        decision_id: str,
        action: str,
        details: dict[str, Any] | None = None,
        expected_order_count: int | None = None,
        pre_state: dict[str, Any] | None = None,
        proposed_state: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        _validate_identifier(decision_id, "decision_id")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("invalid decision action")
        safe_details = _safe_payload(details or {}, label="decision details")
        if expected_order_count is None:
            expected_order_count = safe_details.get("expected_order_count", 0)
        if (
            not isinstance(expected_order_count, int)
            or isinstance(expected_order_count, bool)
            or expected_order_count < 0
        ):
            raise ValueError("invalid expected order count")
        if proposed_state is None and isinstance(safe_details.get("strategy_state"), dict):
            proposed_state = safe_details["strategy_state"]
        timestamp = created_at or _utc_now()
        candidate = {
            "decision_id": decision_id,
            "action": action.strip().upper(),
            "created_at": timestamp,
            "status": "committed" if expected_order_count == 0 else "prepared",
            "status_updated_at": timestamp,
            "expected_order_count": expected_order_count,
            "pre_state": _safe_payload(pre_state, label="decision pre-state"),
            "proposed_state": _safe_payload(proposed_state, label="decision proposed state"),
            "failure": None,
            "details": safe_details,
            "orders": {},
        }
        with self._lock:
            document = self._require(network, symbol)
            existing = document["decisions"].get(decision_id)
            if existing is not None:
                immutable_fields = (
                    "decision_id",
                    "action",
                    "expected_order_count",
                    "pre_state",
                    "proposed_state",
                    "details",
                )
                if any(existing[field] != candidate[field] for field in immutable_fields):
                    raise ExecutionStoreConflict(
                        "decision_id was reused with different content"
                    )
                return deepcopy(existing)
            document["decisions"][decision_id] = candidate
            self._commit(document)
            return deepcopy(candidate)

    def transition_decision(
        self,
        network: str,
        symbol: str,
        *,
        decision_id: str,
        status: str,
        error: object | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Advance a decision lifecycle without allowing terminal-state rewrites."""

        if status not in DECISION_STATUSES:
            raise ValueError("invalid decision status")
        with self._lock:
            document = self._require(network, symbol)
            decision = document["decisions"].get(decision_id)
            if decision is None:
                raise ExecutionStoreError("unknown decision cannot be transitioned")
            current = decision["status"]
            if current == status:
                return deepcopy(decision)
            if status not in _DECISION_TRANSITIONS[current]:
                raise ExecutionStoreConflict(
                    f"invalid decision transition from {current} to {status}"
                )
            if status == "committed":
                self._validate_commit_ready(decision)
            decision["status"] = status
            decision["status_updated_at"] = updated_at or _utc_now()
            decision["failure"] = (
                redact_error(error) if status in {"failed", "recovery_required"} else None
            )
            self._commit(document)
            return deepcopy(decision)

    def committed_decisions(self, network: str, symbol: str) -> list[dict[str, Any]]:
        """Return only decisions whose proposed state is safe for recovery."""

        document = self.load(network, symbol)
        if document is None:
            return []
        return [
            deepcopy(decision)
            for decision in document["decisions"].values()
            if decision["status"] == "committed"
        ]

    def require_resolved(self, network: str, symbol: str) -> dict[str, Any] | None:
        """Load a journal only when no decision needs execution or reconciliation."""

        document = self.load(network, symbol)
        if document is None:
            return None
        unresolved = [
            decision["decision_id"]
            for decision in document["decisions"].values()
            if decision["status"] not in RESOLVED_DECISION_STATUSES
        ]
        if unresolved:
            raise ExecutionStoreError("execution journal contains unresolved decisions")
        return document

    def record_order_attempt(
        self,
        network: str,
        symbol: str,
        *,
        decision_id: str,
        ordinal: int,
        request: dict[str, Any],
        attempted_at: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise ValueError("invalid order ordinal")
        safe_request = _safe_payload(request, label="order request")
        candidate = {
            "ordinal": ordinal,
            "attempted_at": attempted_at or _utc_now(),
            "request": safe_request,
            "result": {"status": "pending", "updated_at": attempted_at or _utc_now()},
        }
        with self._lock:
            document = self._require(network, symbol)
            decision = document["decisions"].get(decision_id)
            if decision is None:
                raise ExecutionStoreError("order attempt references an unknown decision")
            if decision["status"] == "prepared":
                decision["status"] = "executing"
                decision["status_updated_at"] = attempted_at or _utc_now()
            elif decision["status"] != "executing":
                raise ExecutionStoreConflict(
                    "orders cannot be added to a resolved decision"
                )
            if ordinal >= decision["expected_order_count"]:
                raise ExecutionStoreConflict("order ordinal exceeds expected order count")
            key = str(ordinal)
            existing = decision["orders"].get(key)
            if existing is not None:
                if existing["request"] != safe_request:
                    raise ExecutionStoreConflict(
                        "decision_id and ordinal were reused with a different request"
                    )
                return deepcopy(existing)
            decision["orders"][key] = candidate
            self._commit(document)
            return deepcopy(candidate)

    def record_order_result(
        self,
        network: str,
        symbol: str,
        *,
        decision_id: str,
        ordinal: int,
        status: str,
        exchange_order_id: str | int | None = None,
        client_order_id: str | None = None,
        filled_quantity: str | float | int | None = None,
        error: object | None = None,
        details: dict[str, Any] | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in RESULT_STATUSES - {"pending"}:
            raise ValueError("invalid exchange order status")
        result = {
            "status": status,
            "exchange_order_id": exchange_order_id,
            "client_order_id": client_order_id,
            "filled_quantity": filled_quantity,
            "error": redact_error(error),
            "details": _safe_payload(details or {}, label="order result"),
            "updated_at": updated_at or _utc_now(),
        }
        _safe_payload(result, label="order result")
        with self._lock:
            document = self._require(network, symbol)
            decision = document["decisions"].get(decision_id)
            order = None if decision is None else decision["orders"].get(str(ordinal))
            if order is None:
                raise ExecutionStoreError("order result references an unknown attempt")
            existing = order["result"]
            if existing == result:
                return deepcopy(existing)
            if existing["status"] in TERMINAL_STATUSES:
                raise ExecutionStoreConflict("terminal order result cannot be replaced")
            order["result"] = result
            self._commit(document)
            return deepcopy(result)

    def status_summary(self, network: str, symbol: str) -> dict[str, Any] | None:
        document = self.load(network, symbol)
        if document is None:
            return None
        return {
            "schema_version": document["schema_version"],
            "network": network,
            "symbol": symbol,
            "run_id": document["run"]["run_id"],
            "started_at": document["run"]["started_at"],
            "updated_at": document["updated_at"],
            **document["counters"],
        }

    @staticmethod
    def _validate_commit_ready(decision: dict[str, Any]) -> None:
        orders = decision["orders"]
        if len(orders) != decision["expected_order_count"]:
            raise ExecutionStoreConflict("decision does not contain every expected order")
        expected_ordinals = {str(index) for index in range(decision["expected_order_count"])}
        if set(orders) != expected_ordinals:
            raise ExecutionStoreConflict("decision order ordinals are incomplete")
        if any(order["result"]["status"] != "filled" for order in orders.values()):
            raise ExecutionStoreConflict("decision contains an order that is not fully filled")
        for order in orders.values():
            requested = order["request"].get("quantity")
            filled = order["result"].get("filled_quantity")
            try:
                requested_quantity = Decimal(str(requested))
                filled_quantity = Decimal(str(filled))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ExecutionStoreConflict(
                    "decision order quantities are incomplete"
                ) from exc
            if (
                not requested_quantity.is_finite()
                or requested_quantity <= 0
                or filled_quantity != requested_quantity
            ):
                raise ExecutionStoreConflict(
                    "decision order was not filled for the requested quantity"
                )

    def _require(self, network: str, symbol: str) -> dict[str, Any]:
        document = self.load(network, symbol)
        if document is None:
            raise ExecutionStoreError("execution journal has not been initialized")
        return document

    def _commit(self, document: dict[str, Any]) -> None:
        document["updated_at"] = _utc_now()
        document["counters"] = self._derive_counters(document["decisions"])
        self._save(document)

    def _save(self, document: dict[str, Any]) -> None:
        validated = self._validate_document(
            document, network=document.get("network"), symbol=document.get("symbol")
        )
        try:
            encoded = json.dumps(
                validated,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionStoreError("execution journal is not serializable") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        temporary_name: str | None = None
        path = self.path_for(validated["network"], validated["symbol"])
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self.root
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if hasattr(os, "O_DIRECTORY"):
                descriptor = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise ExecutionStoreError("could not atomically save execution journal") from exc
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def _validate_document(
        self, payload: Any, *, network: Any, symbol: Any
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ExecutionStoreError("execution journal schema is incompatible")
        try:
            _validate_identity(network, symbol)
        except ValueError as exc:
            raise ExecutionStoreError("execution journal identity is invalid") from exc
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ExecutionStoreError("execution journal schema is incompatible")
        if payload.get("network") != network or payload.get("symbol") != symbol:
            raise ExecutionStoreError("execution journal identity is incompatible")
        run = payload.get("run")
        if not isinstance(run, dict) or set(run) != {"run_id", "started_at", "metadata"}:
            raise ExecutionStoreError("execution journal run metadata is invalid")
        try:
            _validate_identifier(run["run_id"], "run_id")
            if not isinstance(run["started_at"], str) or not run["started_at"]:
                raise ValueError
            run["metadata"] = _safe_payload(run["metadata"], label="run metadata")
        except (KeyError, ValueError) as exc:
            raise ExecutionStoreError("execution journal run metadata is invalid") from exc
        decisions = payload.get("decisions")
        if not isinstance(decisions, dict):
            raise ExecutionStoreError("execution journal decisions are invalid")
        for decision_id, decision in decisions.items():
            self._validate_decision(decision_id, decision)
        expected_counters = self._derive_counters(decisions)
        if payload.get("counters") != expected_counters:
            raise ExecutionStoreError("execution journal counters are inconsistent")
        if not isinstance(payload.get("updated_at"), str) or not payload["updated_at"]:
            raise ExecutionStoreError("execution journal timestamp is invalid")
        allowed = {
            "schema_version",
            "network",
            "symbol",
            "run",
            "decisions",
            "counters",
            "updated_at",
        }
        if set(payload) != allowed:
            raise ExecutionStoreError("execution journal schema is incompatible")
        return deepcopy(payload)

    def _migrate_v1(
        self,
        payload: dict[str, Any],
        *,
        network: str,
        symbol: str,
        path: Path,
    ) -> dict[str, Any]:
        """Migrate a validated v1 journal without treating uncertain work as committed."""

        legacy = deepcopy(payload)
        legacy["schema_version"] = SCHEMA_VERSION
        decisions = legacy.get("decisions")
        if not isinstance(decisions, dict):
            raise ExecutionStoreError("execution journal decisions are invalid")
        for decision_id, decision in decisions.items():
            if not isinstance(decision, dict):
                raise ExecutionStoreError("execution journal decision is invalid")
            orders = decision.get("orders")
            details = decision.get("details")
            if not isinstance(orders, dict) or not isinstance(details, dict):
                raise ExecutionStoreError("execution journal decision is invalid")
            statuses = [
                order.get("result", {}).get("status")
                for order in orders.values()
                if isinstance(order, dict)
            ]
            expected = details.get("expected_order_count", len(orders))
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < len(orders):
                raise ExecutionStoreError("execution journal expected order count is invalid")
            if not statuses and expected == 0:
                status = "committed"
            elif (
                len(orders) == expected
                and statuses
                and all(item == "filled" for item in statuses)
                and self._legacy_fills_match(orders)
            ):
                status = "committed"
            elif any(item == "filled" for item in statuses):
                status = "recovery_required"
            elif len(orders) == expected and statuses and all(
                item in {"rejected", "cancelled"} for item in statuses
            ):
                status = "failed"
            else:
                status = "recovery_required"
            timestamp = decision.get("created_at") or legacy.get("updated_at") or _utc_now()
            decision.update(
                {
                    "status": status,
                    "status_updated_at": timestamp,
                    "expected_order_count": expected,
                    "pre_state": None,
                    "proposed_state": details.get("strategy_state"),
                    "failure": (
                        "Migrated legacy decision requires reconciliation"
                        if status == "recovery_required"
                        else "Migrated legacy decision did not execute"
                        if status == "failed"
                        else None
                    ),
                }
            )
        legacy["counters"] = self._derive_counters(decisions)
        validated = self._validate_document(legacy, network=network, symbol=symbol)
        self._save(validated)
        return self._validate_document(
            json.loads(path.read_text(encoding="utf-8")), network=network, symbol=symbol
        )

    @staticmethod
    def _legacy_fills_match(orders: dict[str, Any]) -> bool:
        for order in orders.values():
            try:
                requested = Decimal(str(order["request"]["quantity"]))
                filled = Decimal(str(order["result"]["filled_quantity"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                return False
            if not requested.is_finite() or requested <= 0 or requested != filled:
                return False
        return True

    def _validate_decision(self, decision_id: Any, decision: Any) -> None:
        try:
            _validate_identifier(decision_id, "decision_id")
        except ValueError as exc:
            raise ExecutionStoreError("execution journal decision id is invalid") from exc
        if not isinstance(decision, dict) or set(decision) != {
            "decision_id",
            "action",
            "created_at",
            "status",
            "status_updated_at",
            "expected_order_count",
            "pre_state",
            "proposed_state",
            "failure",
            "details",
            "orders",
        }:
            raise ExecutionStoreError("execution journal decision is invalid")
        if decision.get("decision_id") != decision_id:
            raise ExecutionStoreError("execution journal decision identity is invalid")
        if not isinstance(decision.get("action"), str) or not decision["action"]:
            raise ExecutionStoreError("execution journal decision action is invalid")
        if not isinstance(decision.get("created_at"), str) or not decision["created_at"]:
            raise ExecutionStoreError("execution journal decision timestamp is invalid")
        if decision.get("status") not in DECISION_STATUSES:
            raise ExecutionStoreError("execution journal decision status is invalid")
        if not isinstance(decision.get("status_updated_at"), str) or not decision["status_updated_at"]:
            raise ExecutionStoreError("execution journal decision status timestamp is invalid")
        expected_order_count = decision.get("expected_order_count")
        if (
            not isinstance(expected_order_count, int)
            or isinstance(expected_order_count, bool)
            or expected_order_count < 0
        ):
            raise ExecutionStoreError("execution journal expected order count is invalid")
        try:
            _safe_payload(decision.get("details"), label="decision details")
            _safe_payload(decision.get("pre_state"), label="decision pre-state")
            _safe_payload(decision.get("proposed_state"), label="decision proposed state")
            _safe_payload(decision.get("failure"), label="decision failure")
        except ValueError as exc:
            raise ExecutionStoreError("execution journal decision details are invalid") from exc
        orders = decision.get("orders")
        if not isinstance(orders, dict):
            raise ExecutionStoreError("execution journal orders are invalid")
        for ordinal, order in orders.items():
            if ordinal != str(order.get("ordinal")):
                raise ExecutionStoreError("execution journal order identity is invalid")
            self._validate_order(order)
        if len(orders) > expected_order_count:
            raise ExecutionStoreError("execution journal contains excess orders")
        status = decision["status"]
        if status == "prepared" and orders:
            raise ExecutionStoreError("prepared execution decision already contains orders")
        if status == "committed":
            try:
                self._validate_commit_ready(decision)
            except ExecutionStoreConflict as exc:
                raise ExecutionStoreError("committed execution decision is incomplete") from exc
        if status == "failed" and decision.get("failure") is None:
            raise ExecutionStoreError("failed execution decision has no failure reason")

    def _validate_order(self, order: Any) -> None:
        if not isinstance(order, dict) or set(order) != {
            "ordinal",
            "attempted_at",
            "request",
            "result",
        }:
            raise ExecutionStoreError("execution journal order is invalid")
        ordinal = order.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise ExecutionStoreError("execution journal order ordinal is invalid")
        if not isinstance(order.get("attempted_at"), str) or not order["attempted_at"]:
            raise ExecutionStoreError("execution journal order timestamp is invalid")
        try:
            _safe_payload(order.get("request"), label="order request")
        except ValueError as exc:
            raise ExecutionStoreError("execution journal order request is invalid") from exc
        result = order.get("result")
        if not isinstance(result, dict) or result.get("status") not in RESULT_STATUSES:
            raise ExecutionStoreError("execution journal order result is invalid")
        pending_fields = {"status", "updated_at"}
        completed_fields = {
            "status",
            "exchange_order_id",
            "client_order_id",
            "filled_quantity",
            "error",
            "details",
            "updated_at",
        }
        expected_fields = pending_fields if result.get("status") == "pending" else completed_fields
        if set(result) != expected_fields:
            raise ExecutionStoreError("execution journal order result is invalid")
        if not isinstance(result.get("updated_at"), str) or not result["updated_at"]:
            raise ExecutionStoreError("execution journal order result timestamp is invalid")
        try:
            _safe_payload(result, label="order result")
        except ValueError as exc:
            raise ExecutionStoreError("execution journal order result is invalid") from exc

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        return {
            "decision_count": 0,
            "order_attempt_count": 0,
            "submitted_order_count": 0,
            "filled_order_count": 0,
            "rejected_order_count": 0,
            "unknown_order_count": 0,
        }

    @classmethod
    def _derive_counters(cls, decisions: dict[str, Any]) -> dict[str, int]:
        counters = cls._empty_counters()
        counters["decision_count"] = len(decisions)
        for decision in decisions.values():
            for order in decision["orders"].values():
                counters["order_attempt_count"] += 1
                status = order["result"]["status"]
                if status in {"submitted", "partially_filled", "filled"}:
                    counters["submitted_order_count"] += 1
                if status == "filled":
                    counters["filled_order_count"] += 1
                elif status == "rejected":
                    counters["rejected_order_count"] += 1
                elif status == "unknown":
                    counters["unknown_order_count"] += 1
        return counters
