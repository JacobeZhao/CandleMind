"""Bounded retry execution for Binance reads and write reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import random
from threading import Lock
import time
from typing import Callable, TypeVar

from .binance_errors import (
    BinanceFailure,
    BinanceSubmissionOutcome,
    classify_binance_failure,
)


T = TypeVar("T")


class BinanceOperation(str, Enum):
    READ = "read"
    WRITE_RECONCILE = "write_reconcile"
    WRITE_SUBMIT = "write_submit"


@dataclass(frozen=True)
class BinanceRetryPolicy:
    max_attempts: int = 3
    budget_seconds: float = 5.0
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    default_ban_cooldown_seconds: float = 120.0


class ProcessCooldown:
    """A process-wide deadline used to coordinate Binance HTTP 418 backoff."""

    def __init__(self) -> None:
        self._deadline = 0.0
        self._lock = Lock()

    def remaining(self, now: float) -> float:
        with self._lock:
            return max(0.0, self._deadline - now)

    def extend(self, deadline: float) -> None:
        with self._lock:
            self._deadline = max(self._deadline, deadline)

    def reset(self) -> None:
        with self._lock:
            self._deadline = 0.0


PROCESS_BINANCE_COOLDOWN = ProcessCooldown()


class BinanceCooldownActive(RuntimeError):
    """Safe synthetic failure raised when the process cooldown exceeds a call budget."""

    status_code = 418
    code = None
    message = "Binance process cooldown is active"

    def __init__(self) -> None:
        super().__init__(self.message)
        self.response = None


class BinanceRetryExecutor:
    def __init__(
        self,
        *,
        policy: BinanceRetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        cooldown: ProcessCooldown = PROCESS_BINANCE_COOLDOWN,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy or BinanceRetryPolicy()
        if self.policy.max_attempts < 1 or self.policy.budget_seconds < 0:
            raise ValueError("invalid Binance retry policy")
        self.clock = clock
        self.sleeper = sleeper
        self.rng = rng
        self.cooldown = cooldown
        self.wall_clock = wall_clock

    def run(self, operation: BinanceOperation, call: Callable[[], T]) -> T:
        started = self.clock()
        for attempt in range(1, self.policy.max_attempts + 1):
            cooldown_delay = self.cooldown.remaining(self.clock())
            if cooldown_delay and not self._sleep_within_budget(started, cooldown_delay):
                raise BinanceCooldownActive()
            try:
                return call()
            except Exception as exc:
                failure = classify_binance_failure(exc)
                retryable = failure.retryable and (
                    operation is not BinanceOperation.WRITE_SUBMIT
                    or failure.submission_outcome
                    is BinanceSubmissionOutcome.NOT_ACCEPTED
                )
                if not retryable or attempt == self.policy.max_attempts:
                    raise
                delay = self._delay(attempt, failure)
                if failure.status_code in {418, 429}:
                    self.cooldown.extend(self.clock() + delay)
                    delay = self.cooldown.remaining(self.clock())
                if not self._sleep_within_budget(started, delay):
                    raise
        raise AssertionError("retry loop exhausted unexpectedly")

    def _delay(self, attempt: int, failure: BinanceFailure) -> float:
        ceiling = min(
            self.policy.max_delay_seconds,
            self.policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        random_value = self.rng() if callable(self.rng) else self.rng.random()
        jitter = max(0.0, min(1.0, float(random_value))) * ceiling
        if failure.status_code == 418 and failure.retry_after is None:
            if failure.ban_until_ms is not None:
                return max(0.0, failure.ban_until_ms / 1000 - self.wall_clock())
            return self.policy.default_ban_cooldown_seconds
        return max(jitter, failure.retry_after or 0.0)

    def _sleep_within_budget(self, started: float, delay: float) -> bool:
        remaining = self.policy.budget_seconds - (self.clock() - started)
        if delay > remaining:
            return False
        if delay > 0:
            self.sleeper(delay)
        return True
