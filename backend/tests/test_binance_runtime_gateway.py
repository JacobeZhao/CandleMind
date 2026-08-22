from __future__ import annotations

from types import SimpleNamespace

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError

from backend.app.services.binance_errors import (
    BinanceFailureCategory,
    BinanceGatewayAuthenticationError,
    BinanceGatewayRejected,
    BinanceGatewayUnavailable,
    BinanceSubmissionOutcome,
    classify_binance_failure,
)
from backend.app.services.binance_retry import (
    BinanceCooldownActive,
    BinanceOperation,
    BinanceRetryExecutor,
    BinanceRetryPolicy,
    ProcessCooldown,
)
from backend.app.services.binance_usdm_gateway import BinanceUsdMGateway
from backend.app.services.binance_usdm_gateway import (
    gateway_error_detail,
    gateway_error_status,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def failure(
    *, status: int | None = None, code: int | None = None, message: str = "private"
) -> Exception:
    error = RuntimeError("secret request and credential material")
    error.status_code = status
    error.code = code
    error.message = message
    error.response = SimpleNamespace(status_code=status, headers={})
    return error


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_approved_http_failures_are_retryable(status):
    assert classify_binance_failure(failure(status=status)).retryable is True


@pytest.mark.parametrize("code", [-1000, -1001, -1003, -1006, -1007, -1008, -1015])
def test_clearly_transient_binance_codes_are_retryable_even_with_http_400(code):
    assert classify_binance_failure(failure(status=400, code=code)).retryable is True


def test_tls_auth_waf_and_invalid_input_are_not_retryable():
    assert classify_binance_failure(SSLError("certificate secret")).category == BinanceFailureCategory.TLS
    for status in (400, 401, 422):
        assert classify_binance_failure(failure(status=status)).retryable is False
    waf = classify_binance_failure(failure(status=403))
    assert waf.category == BinanceFailureCategory.WAF
    assert waf.safe_message == "Binance infrastructure policy rejected the request"


def test_minus_2015_is_ambiguous_and_not_misclassified_as_geo():
    classified = classify_binance_failure(
        failure(status=400, code=-2015, message="Invalid API-key, IP, or permissions")
    )
    assert classified.category == BinanceFailureCategory.AUTHENTICATION
    assert classified.retryable is False
    assert "geo" not in classified.safe_message.lower()
    assert "region" not in classified.safe_message.lower()


@pytest.mark.parametrize(
    ("status", "message"),
    [(451, "private"), (400, "Service unavailable from a restricted location")],
)
def test_geo_requires_explicit_status_or_message(status, message):
    assert classify_binance_failure(failure(status=status, message=message)).category == (
        BinanceFailureCategory.GEO_RESTRICTED
    )


def test_retry_is_bounded_to_three_attempts_with_full_jitter():
    clock = FakeClock()
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise RequestsConnectionError("private endpoint")

    executor = BinanceRetryExecutor(
        clock=clock, sleeper=clock.sleep, rng=lambda: 0.5, cooldown=ProcessCooldown()
    )
    with pytest.raises(RequestsConnectionError):
        executor.run(BinanceOperation.READ, unavailable)

    assert calls == 3
    assert clock.sleeps == [0.125, 0.25]


def test_retry_after_over_budget_prevents_another_attempt():
    clock = FakeClock()
    calls = 0
    error = failure(status=429)
    error.response.headers["Retry-After"] = "6"

    def rate_limited():
        nonlocal calls
        calls += 1
        raise error

    executor = BinanceRetryExecutor(
        clock=clock, sleeper=clock.sleep, rng=lambda: 0, cooldown=ProcessCooldown()
    )
    with pytest.raises(RuntimeError):
        executor.run(BinanceOperation.READ, rate_limited)

    assert calls == 1
    assert clock.sleeps == []


def test_retry_after_header_is_case_insensitive():
    error = failure(status=429)
    error.response.headers["retry-after"] = "2"
    assert classify_binance_failure(error).retry_after == 2.0


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Unknown error, please check your request or try again later.",
            BinanceSubmissionOutcome.UNKNOWN,
        ),
        ("Service Unavailable.", BinanceSubmissionOutcome.NOT_ACCEPTED),
        (
            "Internal error; unable to process your request. Please try again.",
            BinanceSubmissionOutcome.NOT_ACCEPTED,
        ),
    ],
)
def test_http_503_submission_certainty_uses_documented_message(message, expected):
    classified = classify_binance_failure(failure(status=503, message=message))
    assert classified.submission_outcome is expected


def test_minus_1008_is_known_not_accepted():
    classified = classify_binance_failure(failure(status=503, code=-1008))
    assert classified.submission_outcome is BinanceSubmissionOutcome.NOT_ACCEPTED


def test_http_418_cooldown_is_shared_by_executors():
    clock = FakeClock()
    cooldown = ProcessCooldown()
    first_calls = 0
    error = failure(status=418)
    error.response.headers["Retry-After"] = "2"

    def initially_banned():
        nonlocal first_calls
        first_calls += 1
        if first_calls == 1:
            raise error
        return "ok"

    first = BinanceRetryExecutor(
        clock=clock, sleeper=lambda _seconds: None, rng=lambda: 0, cooldown=cooldown
    )
    second = BinanceRetryExecutor(
        clock=clock, sleeper=clock.sleep, rng=lambda: 0, cooldown=cooldown
    )

    assert first.run(BinanceOperation.READ, initially_banned) == "ok"
    assert second.run(BinanceOperation.READ, lambda: "second") == "second"
    assert clock.sleeps == [2.0]


def test_shared_cooldown_longer_than_budget_blocks_the_request():
    clock = FakeClock()
    cooldown = ProcessCooldown()
    cooldown.extend(6.0)
    calls = 0

    def request():
        nonlocal calls
        calls += 1

    executor = BinanceRetryExecutor(
        clock=clock, sleeper=clock.sleep, rng=lambda: 0, cooldown=cooldown
    )
    with pytest.raises(BinanceCooldownActive):
        executor.run(BinanceOperation.READ, request)
    assert calls == 0
    assert clock.sleeps == []


def test_http_418_without_retry_after_opens_a_safe_default_cooldown():
    clock = FakeClock()
    cooldown = ProcessCooldown()
    calls = 0

    def banned():
        nonlocal calls
        calls += 1
        raise failure(status=418)

    executor = BinanceRetryExecutor(
        clock=clock,
        sleeper=clock.sleep,
        rng=lambda: 0,
        cooldown=cooldown,
    )
    with pytest.raises(RuntimeError):
        executor.run(BinanceOperation.READ, banned)

    assert calls == 1
    assert cooldown.remaining(clock()) == 120.0
    assert clock.sleeps == []


def test_http_418_uses_banned_until_timestamp_for_shared_cooldown():
    clock = FakeClock()
    cooldown = ProcessCooldown()
    calls = 0

    def banned_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure(
                status=418,
                code=-1003,
                message="Way too many requests; IP banned until 1800000005000.",
            )
        return "ok"

    executor = BinanceRetryExecutor(
        policy=BinanceRetryPolicy(budget_seconds=6),
        clock=clock,
        wall_clock=lambda: 1_800_000_000.0,
        sleeper=clock.sleep,
        rng=lambda: 0,
        cooldown=cooldown,
    )

    assert executor.run(BinanceOperation.READ, banned_once) == "ok"
    assert clock.sleeps == [5.0]


def test_http_429_cooldown_is_visible_to_other_executors():
    clock = FakeClock()
    cooldown = ProcessCooldown()
    error = failure(status=429)
    error.response.headers["Retry-After"] = "2"
    calls = 0

    def limited_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return "ok"

    first = BinanceRetryExecutor(
        clock=clock,
        sleeper=clock.sleep,
        rng=lambda: 0,
        cooldown=cooldown,
    )
    assert first.run(BinanceOperation.READ, limited_once) == "ok"
    assert clock.sleeps == [2.0]


def test_write_submission_is_never_retried():
    calls = 0

    def submit():
        nonlocal calls
        calls += 1
        raise RequestsConnectionError("ambiguous submission outcome")

    with pytest.raises(RequestsConnectionError):
        BinanceRetryExecutor(cooldown=ProcessCooldown()).run(
            BinanceOperation.WRITE_SUBMIT, submit
        )
    assert calls == 1


def test_gateway_retries_reads_and_validates_list_payloads():
    calls = 0

    def open_orders(**_params):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise failure(status=503)
        return [{"orderId": 7}]

    clock = FakeClock()
    retry = BinanceRetryExecutor(
        clock=clock, sleeper=clock.sleep, rng=lambda: 0, cooldown=ProcessCooldown()
    )
    gateway = BinanceUsdMGateway(
        SimpleNamespace(futures_get_open_orders=open_orders), retry_executor=retry
    )
    assert gateway.open_orders("BTCUSDT") == [{"orderId": 7}]
    assert calls == 3

    gateway.client.futures_get_open_orders = lambda **_params: {"orderId": 7}
    with pytest.raises(BinanceGatewayRejected) as captured:
        gateway.open_orders("BTCUSDT")
    assert captured.value.failure.category == BinanceFailureCategory.INVALID_RESPONSE


def test_gateway_exposes_typed_dict_and_list_reads():
    client = SimpleNamespace(
        futures_account=lambda: {"assets": []},
        futures_exchange_info=lambda: {"symbols": []},
        futures_mark_price=lambda **_params: {"markPrice": "1"},
        futures_account_balance=lambda: [{"asset": "USDT"}],
        futures_position_information=lambda **_params: [{"symbol": "BTCUSDT"}],
        futures_time=lambda: {"serverTime": 123},
        futures_ping=lambda: {},
        futures_klines=lambda **_params: [[1, "2"]],
        futures_symbol_ticker=lambda **_params: {"price": "2"},
        futures_ticker=lambda **_params: {"priceChangePercent": "1"},
        futures_funding_rate=lambda **_params: [{"fundingRate": "0.1"}],
        futures_get_position_mode=lambda: {"dualSidePosition": False},
        futures_get_order=lambda **_params: {"orderId": 7},
        futures_income_history=lambda **_params: [{"income": "3"}],
    )
    gateway = BinanceUsdMGateway(client)

    assert gateway.account() == {"assets": []}
    assert gateway.exchange_info() == {"symbols": []}
    assert gateway.mark_price(symbol="BTCUSDT") == {"markPrice": "1"}
    assert gateway.account_balance() == [{"asset": "USDT"}]
    assert gateway.position_information(symbol="BTCUSDT") == [{"symbol": "BTCUSDT"}]
    assert gateway.server_time() == 123
    assert gateway.ping() == {}
    assert gateway.klines(symbol="BTCUSDT", interval="1m") == [[1, "2"]]
    assert gateway.symbol_ticker(symbol="BTCUSDT") == {"price": "2"}
    assert gateway.ticker(symbol="BTCUSDT") == {"priceChangePercent": "1"}
    assert gateway.funding_rate(symbol="BTCUSDT") == [{"fundingRate": "0.1"}]
    assert gateway.position_mode() == {"dualSidePosition": False}
    assert gateway.order(symbol="BTCUSDT", orderId=7) == {"orderId": 7}
    assert gateway.income_history(symbol="BTCUSDT") == [{"income": "3"}]


def test_gateway_invalid_payload_identifies_operation():
    gateway = BinanceUsdMGateway(SimpleNamespace(futures_time=lambda: {"serverTime": "bad"}))

    with pytest.raises(BinanceGatewayRejected) as captured:
        gateway.server_time()

    assert captured.value.operation == "server_time"
    assert captured.value.failure.category == BinanceFailureCategory.INVALID_RESPONSE


def test_gateway_errors_are_sanitized_and_preserve_existing_types():
    secret = "api-key=do-not-leak"
    client = SimpleNamespace(futures_get_all_orders=lambda **_params: (_ for _ in ()).throw(
        failure(status=400, code=-2015, message=secret)
    ))

    with pytest.raises(BinanceGatewayAuthenticationError) as captured:
        BinanceUsdMGateway(client).all_orders(symbol="BTCUSDT")
    assert secret not in str(captured.value)

    client.futures_get_all_orders = lambda **_params: (_ for _ in ()).throw(
        failure(status=503, message=secret)
    )
    retry = BinanceRetryExecutor(
        clock=FakeClock(), sleeper=lambda _seconds: None, rng=lambda: 0, cooldown=ProcessCooldown()
    )
    with pytest.raises(BinanceGatewayUnavailable) as captured:
        BinanceUsdMGateway(client, retry_executor=retry).all_orders(symbol="BTCUSDT")
    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_code"),
    [
        (429, 429, "binance_rate_limited"),
        (451, 451, "binance_geo_restricted"),
        (503, 503, "binance_unavailable"),
    ],
)
def test_gateway_public_contract_preserves_actionable_failure_category(
    status, expected_status, expected_code
):
    classified = classify_binance_failure(failure(status=status))
    error = BinanceGatewayUnavailable(
        classified.safe_message,
        failure=classified,
    )

    assert gateway_error_status(error) == expected_status
    detail = gateway_error_detail(error)
    assert detail["code"] == expected_code
    assert "private" not in detail["message"]
