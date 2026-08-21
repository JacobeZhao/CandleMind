"""Domain contracts for the persistent market-agent harness ledger."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any


NETWORKS = frozenset({"testnet", "mainnet"})
_SYMBOL = re.compile(r"^[A-Z0-9]{2,16}USDT$")


class JobLane(StrEnum):
    MARKET = "market"
    INBOX = "inbox"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    PUBLISHED = "published"
    FAILED = "failed"
    SUPERSEDED = "superseded"


TERMINAL_JOB_STATES = frozenset(
    {JobState.PUBLISHED, JobState.FAILED, JobState.SUPERSEDED}
)
CLAIMABLE_JOB_STATES = frozenset({JobState.PENDING, JobState.RETRY_WAIT})


class MarketAgentLedgerError(RuntimeError):
    """Base error for invalid or unavailable ledger operations."""


class MarketAgentLedgerConflict(MarketAgentLedgerError):
    """Raised when an idempotency key is reused with different input."""


class MarketAgentLeaseError(MarketAgentLedgerError):
    """Raised when a worker does not own the active job lease."""


@dataclass(frozen=True, slots=True)
class MarketAgentJob:
    id: str
    network: str
    symbol: str
    lane: JobLane
    dedupe_key: str
    state: JobState
    priority: int
    payload: dict[str, Any]
    reasons: tuple[str, ...]
    result: dict[str, Any] | None
    attempts: int
    available_at_ms: int
    lease_owner: str | None
    lease_expires_at_ms: int | None
    error_code: str | None
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class MarketAgentEvent:
    sequence: int
    job_id: str
    network: str
    symbol: str
    event_type: str
    role: str
    content: str
    structured: dict[str, Any]
    reasons: tuple[str, ...]
    created_at_ms: int
    published_at_ms: int | None


def normalize_scope(network: str, symbol: str) -> tuple[str, str]:
    normalized_network = str(network).strip().lower()
    normalized_symbol = str(symbol).strip().upper()
    if normalized_network not in NETWORKS:
        raise ValueError("network must be testnet or mainnet")
    if not _SYMBOL.fullmatch(normalized_symbol):
        raise ValueError("symbol must be an uppercase USDT futures symbol")
    return normalized_network, normalized_symbol


def bounded_text(value: Any, *, name: str, maximum: int, allow_empty: bool = False) -> str:
    if value is None:
        raise ValueError(f"{name} cannot be empty")
    text = str(value).strip()
    if not allow_empty and not text:
        raise ValueError(f"{name} cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return text


__all__ = [
    "CLAIMABLE_JOB_STATES",
    "JobLane",
    "JobState",
    "MarketAgentEvent",
    "MarketAgentJob",
    "MarketAgentLedgerConflict",
    "MarketAgentLedgerError",
    "MarketAgentLeaseError",
    "NETWORKS",
    "TERMINAL_JOB_STATES",
    "bounded_text",
    "normalize_scope",
]
