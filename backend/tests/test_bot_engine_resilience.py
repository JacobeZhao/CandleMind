import asyncio
from http.client import RemoteDisconnected
from types import SimpleNamespace

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout

from backend.app.services import bot_engine as bot_engine_module
from backend.app.services.bot_engine import BotEngine
from backend.app.services.paper_broker import PaperBroker
from backend.app.services.sar_adx_runtime import SarAdxRuntimeError
from backend.app.services.sar_adx_state_store import SarAdxStateError
from backend.app.strategies.sar_pyramid import SarPyramidConfig


class _Runtime:
    def __init__(self, error=None):
        self.error = error
        self.process_calls = 0

    def process_bars(self, *_args, **_kwargs):
        self.process_calls += 1
        if self.error is not None:
            raise self.error
        return []

    def status(self, _mark_price):
        return {
            "direction": 0,
            "last_processed_bar": None,
            "paper_equity": 10_000.0,
            "paper_fill_count": 0,
            "paper_fill_count_complete": True,
        }


class _SnapshotClient:
    def __init__(self, failures=0, error_factory=None):
        self.failures = failures
        self.error_factory = error_factory or (
            lambda: RequestsConnectionError("proxy credentials and URL")
        )
        self.calls = []

    def futures_klines(self, **_kwargs):
        self.calls.append("klines")
        if self.failures:
            self.failures -= 1
            raise self.error_factory()
        return [[0, "100", "101", "99", "100", "1", 299999,
                 "100", 1, "0.5", "50", "0"]]

    def futures_time(self):
        self.calls.append("server_time")
        return {"serverTime": 0}

    def futures_funding_rate(self, **_kwargs):
        self.calls.append("funding_rate")
        return []

    def futures_exchange_info(self):
        self.calls.append("exchange_info")
        return {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING"}]}

    def futures_mark_price(self, **_kwargs):
        self.calls.append("mark_price")
        return {"markPrice": "100"}


def _run_loop(engine, client, runtime, monkeypatch):
    observed = []
    real_sleep = asyncio.sleep

    async def immediate_sleep(_delay):
        observed.append((engine.running, engine.engine_state, engine.failure_count))
        await real_sleep(0)

    monkeypatch.setattr(bot_engine_module.asyncio, "sleep", immediate_sleep)

    async def scenario():
        engine.running = True
        engine.last_action = "[SAR+ADX paper] OPEN LONG 1 @ 100"
        task = asyncio.create_task(engine._loop(client, "SOLUSDT", runtime, 0))
        engine._task = task
        await task

    asyncio.run(scenario())
    return observed


def test_transient_snapshot_failure_retries_full_snapshot_and_recovers(monkeypatch):
    engine = BotEngine()
    engine.running = True
    client = _SnapshotClient(failures=1)
    runtime = _Runtime()
    observed = []

    async def immediate_sleep(_delay):
        observed.append((engine.running, engine.engine_state, engine.failure_count))

    monkeypatch.setattr(bot_engine_module.asyncio, "sleep", immediate_sleep)
    result = asyncio.run(engine._cycle_with_retry(client, "SOLUSDT", runtime))

    assert result.last_action == ""
    assert client.calls.count("klines") == 2
    assert client.calls.count("server_time") == 2
    assert client.calls.count("funding_rate") == 2
    assert client.calls.count("exchange_info") == 2
    assert client.calls.count("mark_price") == 2
    assert runtime.process_calls == 1
    assert observed == [(True, "retrying", 1)]
    assert engine.engine_state == "running"
    assert engine.failure_count == 0
    assert engine.error_code is None
    assert engine.error_msg == ""
    assert engine.next_retry_at is None
    assert engine.last_success_at is not None


@pytest.mark.parametrize("error_type", [RequestsTimeout, RequestsConnectionError])
def test_retry_exhaustion_halts_with_sanitized_network_state(monkeypatch, error_type):
    engine = BotEngine()
    client = _SnapshotClient(
        failures=10,
        error_factory=lambda: error_type("secret proxy detail"),
    )
    runtime = _Runtime()

    observed = _run_loop(engine, client, runtime, monkeypatch)

    assert client.calls.count("klines") == 3
    assert runtime.process_calls == 0
    assert any(running and state == "retrying" for running, state, _ in observed)
    assert engine.running is False
    assert engine.engine_state == "network_halted"
    assert engine.failure_count == 3
    assert engine.error_code == "network_unavailable"
    assert engine.error_msg == "Binance is temporarily unavailable"
    assert "secret" not in engine.error_msg
    assert engine.last_action == "[SAR+ADX paper] OPEN LONG 1 @ 100"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, "request_timeout"),
        (429, "rate_limited"),
        (500, "upstream_unavailable"),
        (503, "upstream_unavailable"),
        (401, None),
        (403, None),
        (422, None),
    ],
)
def test_only_approved_http_statuses_are_retryable(status, expected):
    error = type("HttpFailure", (Exception,), {"status_code": status})()
    assert BotEngine._retry_details(error) == expected


@pytest.mark.parametrize(
    "error",
    [RemoteDisconnected("closed"), ConnectionResetError("reset")],
)
def test_direct_connection_failures_are_retryable(error):
    assert BotEngine._retry_details(error) == "network_unavailable"


def test_tls_certificate_failure_is_not_retryable():
    assert BotEngine._retry_details(RequestsSSLError("certificate failed")) is None


def test_rate_limit_retry_after_must_fit_the_short_retry_window():
    short = type(
        "RateLimit",
        (Exception,),
        {"status_code": 429, "response": SimpleNamespace(headers={"Retry-After": "2"})},
    )()
    long = type(
        "RateLimit",
        (Exception,),
        {"status_code": 429, "response": SimpleNamespace(headers={"Retry-After": "30"})},
    )()

    assert BotEngine._retry_delay(1, short) == 2.0
    assert BotEngine._retry_delay(1, long) is None


def test_runtime_failure_is_not_retried_and_requires_recovery(monkeypatch):
    engine = BotEngine()
    client = _SnapshotClient()
    runtime = _Runtime(SarAdxRuntimeError("execution window expired: secret"))

    _run_loop(engine, client, runtime, monkeypatch)

    assert client.calls.count("klines") == 1
    assert runtime.process_calls == 1
    assert engine.running is False
    assert engine.engine_state == "recovery_required"
    assert engine.error_code == "runtime_recovery_required"
    assert engine.error_msg == "Paper strategy state requires recovery"
    assert "secret" not in engine.error_msg
    assert engine.last_action == "[SAR+ADX paper] OPEN LONG 1 @ 100"


def test_unclassified_failure_is_not_retried(monkeypatch):
    engine = BotEngine()
    client = _SnapshotClient()
    runtime = _Runtime(ValueError("unexpected secret"))

    _run_loop(engine, client, runtime, monkeypatch)

    assert client.calls.count("klines") == 1
    assert runtime.process_calls == 1
    assert engine.engine_state == "halted"
    assert engine.error_code == "engine_failure"
    assert engine.error_msg == "Paper strategy stopped unexpectedly"
    assert "secret" not in engine.error_msg


def test_state_store_failure_requires_recovery():
    engine = BotEngine()

    engine._set_terminal_error(SarAdxStateError("corrupt state detail"))

    assert engine.engine_state == "recovery_required"
    assert engine.error_code == "runtime_recovery_required"
    assert "corrupt" not in engine.error_msg


def test_stopped_engine_hydrates_persisted_position_and_fill_count(monkeypatch):
    broker = PaperBroker(10_000.0)
    broker.open(1, 100.0, "d1", SarPyramidConfig())
    payload = {"broker": broker.to_dict()}
    store = SimpleNamespace(load_summary=lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(bot_engine_module, "SarAdxStateStore", lambda: store)
    engine = BotEngine()

    engine.hydrate_persisted_status("SOLUSDT")
    status = engine.status

    assert status["running"] is False
    assert status["position_direction"] == "LONG"
    assert status["paper_fill_count"] == 1
    assert status["paper_fill_count_complete"] is True


def test_no_action_cycle_does_not_replace_last_real_action():
    engine = BotEngine()
    engine.last_action = "[SAR+ADX paper] ADD LONG 1 @ 101"

    engine._apply_cycle(bot_engine_module._CycleResult(102.0, "LONG", "", 0))

    assert engine.last_action == "[SAR+ADX paper] ADD LONG 1 @ 101"
    assert engine.last_signal == "LONG"


def test_cancelling_during_retry_stops_before_another_snapshot(monkeypatch):
    engine = BotEngine()
    engine.running = True
    client = _SnapshotClient(failures=10)
    runtime = _Runtime()
    retry_sleep_entered = asyncio.Event()
    real_sleep = asyncio.sleep

    async def blocked_sleep(_delay):
        retry_sleep_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bot_engine_module.asyncio, "sleep", blocked_sleep)

    async def scenario():
        task = asyncio.create_task(
            engine._cycle_with_retry(client, "SOLUSDT", runtime)
        )
        await retry_sleep_entered.wait()
        calls_before_cancel = len(client.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await real_sleep(0)
        assert len(client.calls) == calls_before_cancel

    asyncio.run(scenario())
    assert client.calls.count("klines") == 1
    assert runtime.process_calls == 0
