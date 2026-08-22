"""Sanitized, structured classification for Binance runtime failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any, Mapping

from binance.exceptions import BinanceAPIException, BinanceRequestException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout


class BinanceFailureCategory(str, Enum):
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UPSTREAM = "upstream"
    TLS = "tls"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_INPUT = "invalid_input"
    GEO_RESTRICTED = "geo_restricted"
    REJECTED = "rejected"
    INVALID_RESPONSE = "invalid_response"
    WAF = "waf"


class BinanceSubmissionOutcome(str, Enum):
    UNKNOWN = "unknown"
    NOT_ACCEPTED = "not_accepted"
    REJECTED = "rejected"


_TRANSIENT_STATUSES = frozenset({408, 418, 429, 500, 502, 503, 504})
_TRANSIENT_BINANCE_CODES = frozenset({-1000, -1001, -1003, -1006, -1007, -1008, -1015})
_GEO_MARKERS = (
    "restricted location",
    "restricted jurisdiction",
    "not available in your country",
    "not available in your region",
    "country of residence",
)


@dataclass(frozen=True)
class BinanceFailure:
    category: BinanceFailureCategory
    retryable: bool
    safe_message: str
    status_code: int | None = None
    exchange_code: int | None = None
    retry_after: float | None = None
    ban_until_ms: int | None = None
    submission_outcome: BinanceSubmissionOutcome = BinanceSubmissionOutcome.UNKNOWN


class BinanceGatewayError(RuntimeError):
    code = "upstream_error"

    def __init__(self, message: str, *, failure: BinanceFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class BinanceGatewayUnavailable(BinanceGatewayError):
    code = "upstream_unavailable"


class BinanceGatewayRejected(BinanceGatewayError):
    code = "upstream_rejected"


class BinanceGatewayAuthenticationError(BinanceGatewayRejected):
    """An intentionally ambiguous credential, permission, or allowlist rejection."""

    code = "authentication_failed"


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def http_status(exc: BaseException) -> int | None:
    direct = _integer(getattr(exc, "status_code", None))
    if direct is not None:
        return direct
    return _integer(getattr(getattr(exc, "response", None), "status_code", None))


def _headers(exc: BaseException) -> Mapping[str, Any]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def retry_after_seconds(exc: BaseException) -> float | None:
    value = next(
        (value for key, value in _headers(exc).items() if str(key).lower() == "retry-after"),
        None,
    )
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _exchange_code(exc: BaseException) -> int | None:
    return _integer(getattr(exc, "code", None))


def _exchange_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    return message.lower() if isinstance(message, str) else ""


def _ban_until_ms(message: str) -> int | None:
    match = re.search(r"\bbanned until\s+(\d{10,16})\b", message)
    if match is None:
        return None
    value = int(match.group(1))
    return value * 1000 if value < 10_000_000_000 else value


def _failure(
    category: BinanceFailureCategory,
    *,
    retryable: bool,
    status: int | None,
    code: int | None,
    retry_after: float | None = None,
    ban_until_ms: int | None = None,
    submission_outcome: BinanceSubmissionOutcome = BinanceSubmissionOutcome.UNKNOWN,
) -> BinanceFailure:
    messages = {
        BinanceFailureCategory.TRANSPORT: "Binance is temporarily unavailable",
        BinanceFailureCategory.TIMEOUT: "Binance request timed out",
        BinanceFailureCategory.RATE_LIMITED: "Binance request rate was limited",
        BinanceFailureCategory.UPSTREAM: "Binance is temporarily unavailable",
        BinanceFailureCategory.TLS: "Binance TLS verification failed",
        BinanceFailureCategory.AUTHENTICATION: "Binance rejected the account credentials or access policy",
        BinanceFailureCategory.PERMISSION: "Binance denied the requested operation",
        BinanceFailureCategory.INVALID_INPUT: "Binance rejected the request parameters",
        BinanceFailureCategory.GEO_RESTRICTED: "Binance is unavailable from the current region",
        BinanceFailureCategory.REJECTED: "Binance rejected the request",
        BinanceFailureCategory.INVALID_RESPONSE: "Binance returned an invalid response",
        BinanceFailureCategory.WAF: "Binance infrastructure policy rejected the request",
    }
    return BinanceFailure(
        category,
        retryable,
        messages[category],
        status,
        code,
        retry_after,
        ban_until_ms,
        submission_outcome,
    )


def classify_binance_failure(exc: BaseException) -> BinanceFailure:
    """Classify an exception without retaining request, credential, or response text."""
    status = http_status(exc)
    code = _exchange_code(exc)
    message = _exchange_message(exc)

    if isinstance(exc, RequestsSSLError):
        return _failure(BinanceFailureCategory.TLS, retryable=False, status=status, code=code)
    if isinstance(exc, (RequestsTimeout, TimeoutError)):
        return _failure(BinanceFailureCategory.TIMEOUT, retryable=True, status=status, code=code)
    if isinstance(exc, (RequestsConnectionError, ConnectionError)):
        return _failure(BinanceFailureCategory.TRANSPORT, retryable=True, status=status, code=code)
    if status == 451 or any(marker in message for marker in _GEO_MARKERS):
        return _failure(
            BinanceFailureCategory.GEO_RESTRICTED,
            retryable=False,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.REJECTED,
        )
    if status in {401}:
        return _failure(
            BinanceFailureCategory.AUTHENTICATION,
            retryable=False,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.REJECTED,
        )
    if status in {403}:
        return _failure(
            BinanceFailureCategory.WAF,
            retryable=False,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.REJECTED,
        )
    if code == -2015:
        return _failure(
            BinanceFailureCategory.AUTHENTICATION,
            retryable=False,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.REJECTED,
        )
    if status in {418, 429} or code in {-1003, -1015}:
        return _failure(
            BinanceFailureCategory.RATE_LIMITED,
            retryable=True,
            status=status,
            code=code,
            retry_after=retry_after_seconds(exc),
            ban_until_ms=_ban_until_ms(message),
            submission_outcome=BinanceSubmissionOutcome.NOT_ACCEPTED,
        )
    if code == -1008 or (
        status == 503
        and any(
            marker in message
            for marker in (
                "service unavailable",
                "internal error; unable to process your request",
                "request throttled by system-level protection",
            )
        )
    ):
        return _failure(
            BinanceFailureCategory.UPSTREAM,
            retryable=True,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.NOT_ACCEPTED,
        )
    if status == 408 or code == -1007:
        return _failure(BinanceFailureCategory.TIMEOUT, retryable=True, status=status, code=code)
    if status in {500, 502, 503, 504} or code in _TRANSIENT_BINANCE_CODES:
        return _failure(BinanceFailureCategory.UPSTREAM, retryable=True, status=status, code=code)
    if status in {400, 404, 405, 409, 422}:
        return _failure(
            BinanceFailureCategory.INVALID_INPUT,
            retryable=False,
            status=status,
            code=code,
            submission_outcome=BinanceSubmissionOutcome.REJECTED,
        )
    if isinstance(exc, BinanceRequestException):
        return _failure(BinanceFailureCategory.INVALID_RESPONSE, retryable=False, status=status, code=code)
    if isinstance(exc, BinanceAPIException):
        return _failure(BinanceFailureCategory.REJECTED, retryable=False, status=status, code=code)
    if status in _TRANSIENT_STATUSES:
        return _failure(BinanceFailureCategory.UPSTREAM, retryable=True, status=status, code=code)
    return _failure(BinanceFailureCategory.REJECTED, retryable=False, status=status, code=code)


def invalid_response_failure() -> BinanceFailure:
    return _failure(
        BinanceFailureCategory.INVALID_RESPONSE,
        retryable=False,
        status=None,
        code=None,
    )
