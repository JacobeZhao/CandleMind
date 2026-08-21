"""Server-owned lifecycle, scheduling, and persistence for market analysis."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import os
from threading import Lock
from typing import Any
from uuid import uuid4

from cryptography.fernet import InvalidToken
from loguru import logger

from ..database import AIConfig, SessionLocal, Settings
from ..security import decrypt
from ..ws_manager import manager as ws_manager
from .ai_config_validation import AIConfigValidationError, validate_ai_config
from .ai_provider import AIProviderError
from .market_agent_state_store import (
    ANALYSIS_INTERVALS,
    MAX_EVENTS,
    MAX_SUMMARIES,
    TRIGGER_INTERVAL,
    MarketAgentStateError,
    MarketAgentStateStore,
)
from .market_analysis_graph import MAX_ANSWER_LENGTH, MarketAnalysisGraph
from .market_agent_contracts import JobLane, MarketAgentEvent, MarketAgentJob
from .market_agent_queue_store import MarketAgentQueueStore, utc_ms
from .multi_timeframe_market_snapshot import MultiTimeframeMarketDataError
from .read_only_market_gateway import ReadOnlyMarketGateway


SUPPORTED_INTERVALS = {interval: None for interval in ANALYSIS_INTERVALS}
DEFAULT_DAILY_LIMIT = 300
TRIGGER_SECONDS = 300
MAX_MANUAL_MESSAGE_LENGTH = 1_000


class MarketAgentError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, status_code: int = 400, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now(now: datetime | None = None) -> str:
    return (now or _utc_now()).isoformat().replace("+00:00", "Z")


def _daily_limit() -> int:
    raw = os.environ.get("CANDLEMIND_MARKET_AGENT_DAILY_LIMIT", str(DEFAULT_DAILY_LIMIT))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DAILY_LIMIT
    return max(1, value)


def _default_client_getter() -> Any:
    from ..state import app_state

    return app_state.client


class MarketAgentManager:
    def __init__(
        self,
        *,
        state_store: MarketAgentStateStore | None = None,
        graph: MarketAnalysisGraph | None = None,
        session_factory: Callable[[], Any] = SessionLocal,
        client_getter: Callable[[], Any] = _default_client_getter,
        notifier: Callable[[dict[str, Any]], Any] | None = None,
        daily_limit: int | None = None,
        retry_delays: tuple[float, ...] = (5, 10, 20, 40, 80, 160, 300),
        idle_poll_seconds: float = 5.0,
        queue_store: MarketAgentQueueStore | None = None,
    ) -> None:
        self.store = state_store or MarketAgentStateStore()
        self.graph = graph or MarketAnalysisGraph(
            self.store.root / "checkpoints" / "market_analysis.sqlite3"
        )
        self.session_factory = session_factory
        self.client_getter = client_getter
        self.notifier = notifier or ws_manager.broadcast
        self.daily_limit = daily_limit if daily_limit is not None else _daily_limit()
        self.retry_delays = retry_delays or (5, 10, 20, 40, 80, 160, 300)
        self.idle_poll_seconds = max(0.1, idle_poll_seconds)
        self.queue_store = queue_store
        self.worker_id = f"market-agent-worker:{uuid4()}"
        self._inbox_streak = 0
        self._lock = asyncio.Lock()
        self._analysis_lock = asyncio.Lock()
        self._sync_lock = Lock()
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._loaded = False
        self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        now = _iso_now()
        return {
            "desired_enabled": False,
            "agent_id": None,
            "symbol": None,
            "network": None,
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
            "daily_usage_date": _utc_now().date().isoformat(),
            "daily_usage_count": 0,
            "next_sequence": 1,
            "started_at": None,
            "updated_at": now,
            "events": [],
            "summaries": [],
        }

    def _load_once(self) -> None:
        with self._sync_lock:
            if self._loaded:
                return
            try:
                persisted = self.store.load()
            except MarketAgentStateError:
                logger.warning("Market agent state could not be restored")
                persisted = None
            if persisted:
                state = self._empty_state()
                state.update(persisted)
                state["events"] = list(state.get("events", []))[-MAX_EVENTS:]
                state["summaries"] = list(state.get("summaries", []))[-MAX_SUMMARIES:]
                self._state = state
            self._loaded = True

    def _save(self) -> None:
        self._state["updated_at"] = _iso_now()
        self.store.save(self._state)

    def status(self) -> dict[str, Any]:
        self._load_once()
        state = self._state
        result = {
            "agent_id": state["agent_id"],
            "state": state["state"],
            "desired_enabled": bool(state["desired_enabled"]),
            "enabled": bool(state["desired_enabled"]),
            "symbol": state["symbol"],
            "network": state.get("network"),
            "trigger_interval": state["trigger_interval"],
            "interval": state["trigger_interval"],
            "analysis_intervals": list(state["analysis_intervals"]),
            "retry_attempt": int(state["retry_attempt"]),
            "consecutive_failures": int(state["retry_attempt"]),
            "retry_not_before": state["retry_not_before"],
            "paused_reason": state["paused_reason"],
            "last_scheduled_cutoff": state["last_scheduled_cutoff"],
            "last_processed_bar_closed_at": state["last_scheduled_cutoff"],
            "active_batch_id": state["active_batch_id"],
            "latest_sequence": int(state["next_sequence"]) - 1,
            "daily_usage_count": int(state["daily_usage_count"]),
            "daily_usage_limit": self.daily_limit,
            "started_at": state["started_at"],
            "updated_at": state["updated_at"],
        }
        if self.queue_store is not None and state.get("network") and state.get("symbol"):
            result.update(self.queue_store.status_summary(state["network"], state["symbol"]))
        return result

    def events(self, *, after_sequence: int = 0, limit: int = MAX_EVENTS) -> list[dict[str, Any]]:
        self._load_once()
        if self.queue_store is not None and self._state.get("network") and self._state.get("symbol"):
            return [
                self._ledger_event_payload(event)
                for event in self.queue_store.events(
                    after_sequence=after_sequence,
                    limit=limit,
                    network=self._state["network"],
                    symbol=self._state["symbol"],
                )
            ]
        return [
            dict(event)
            for event in self._state["events"]
            if int(event.get("sequence", 0)) > after_sequence
        ][:limit]

    async def start(self, *, symbol: str, interval: str | None = None) -> dict[str, Any]:
        del interval  # Accepted temporarily for clients using the v1 visual-interval contract.
        if self.client_getter() is None:
            raise MarketAgentError(
                "market_unavailable",
                "Binance market service is unavailable",
                status_code=503,
                retryable=True,
            )
        config_id, _, _ = await asyncio.to_thread(self._resolve_config, None)
        try:
            self.store.acquire_worker_lock()
        except MarketAgentStateError as exc:
            raise MarketAgentError(
                "single_worker_required", str(exc), status_code=503
            ) from exc

        async with self._lock:
            self._load_once()
            running = self._task is not None and not self._task.done()
            same_symbol = self._state["symbol"] == symbol
            if running:
                if same_symbol:
                    return self.status()
                raise MarketAgentError(
                    "agent_context_conflict",
                    "Stop the current market agent before changing symbol",
                    status_code=409,
                )

            stopped_to_started = not self._state["desired_enabled"] or self._state["state"] == "stopped"
            if not same_symbol:
                self._state = self._empty_state()
                self._state["agent_id"] = str(uuid4())
                self._state["symbol"] = symbol
            elif not self._state["agent_id"]:
                self._state["agent_id"] = str(uuid4())
            client = self.client_getter()
            self._state.update(
                desired_enabled=True,
                network="testnet" if bool(getattr(client, "testnet", False)) else "mainnet",
                config_id=config_id,
                state="running",
                retry_attempt=0,
                retry_not_before=None,
                paused_reason=None,
                active_batch_id=None,
                active_thread_id=None,
                started_at=_iso_now(),
            )
            self._reset_daily_usage_if_needed()
            self._generation += 1
            generation = self._generation
            self._save()
            runner = self._run_queue if self.queue_store is not None else self._run
            self._task = asyncio.create_task(
                runner(generation, immediate=stopped_to_started),
                name=f"market-agent-{self._state['agent_id']}",
            )
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            self._load_once()
            self._generation += 1
            task = self._task
            self._task = None
            self._state.update(
                desired_enabled=False,
                state="stopped",
                paused_reason=None,
                active_batch_id=None,
                active_thread_id=None,
                retry_attempt=0,
                retry_not_before=None,
            )
            self._save()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.status()

    async def restore(self) -> dict[str, Any]:
        self.store.acquire_worker_lock()
        if self.queue_store is not None:
            await asyncio.to_thread(self.queue_store.initialize)
            await asyncio.to_thread(self.queue_store.recover_expired_leases)
        async with self._lock:
            self._load_once()
            if not self._state["desired_enabled"] or not self._state["symbol"]:
                self._state["state"] = "stopped"
                self._save()
                return self.status()
            if not self._state.get("network"):
                client = self.client_getter()
                self._state["network"] = (
                    "testnet" if bool(getattr(client, "testnet", False)) else "mainnet"
                )
            if self._task is not None and not self._task.done():
                return self.status()
            self._state["state"] = (
                "running" if self.client_getter() is not None else "waiting_market"
            )
            self._state["active_batch_id"] = None
            self._state["active_thread_id"] = None
            self._generation += 1
            generation = self._generation
            self._save()
            runner = self._run_queue if self.queue_store is not None else self._run
            self._task = asyncio.create_task(
                runner(generation, immediate=False),
                name=f"market-agent-{self._state['agent_id']}",
            )
            return self.status()

    async def shutdown(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            self._generation += 1
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.graph.close()
        self.store.release_worker_lock()

    async def message(
        self, *, symbol: str, content: str, client_message_id: str | None = None
    ) -> dict[str, Any]:
        message = content.strip()
        if not message or len(message) > MAX_MANUAL_MESSAGE_LENGTH:
            raise MarketAgentError("invalid_message", "Message must contain 1 to 1000 characters")
        async with self._lock:
            self._load_once()
            if not self._state["desired_enabled"] or self._state["symbol"] != symbol:
                raise MarketAgentError(
                    "agent_context_conflict",
                    "Start the market agent for this symbol before sending a message",
                    status_code=409,
                )
            if self.queue_store is not None:
                job = await asyncio.to_thread(
                    self.queue_store.enqueue_inbox_message,
                    self._state["network"],
                    symbol,
                    client_message_id or str(uuid4()),
                    message,
                    priority=100,
                )
                return {
                    "accepted": True,
                    "job_id": job.id,
                    "state": job.state.value,
                    "client_message_id": job.payload.get("client_message_id"),
                }
            generation = self._generation
            user_event = self._append_event(
                event_type="user_message",
                role="user",
                content=message,
                batch_id=f"manual-user:{uuid4()}",
                cutoff=self._state["last_scheduled_cutoff"],
                reasons=[],
                context={},
            )
            self._append_summary(
                {"role": "user", "content": message[:600], "batch_id": user_event["batch_id"]}
            )
            self._save()
        await self._notify_event(user_event)
        try:
            event = await self._process_batch(
                self.client_getter(), generation, mode="manual", manual_query=message
            )
        except AIProviderError as exc:
            raise MarketAgentError(
                exc.code,
                exc.message,
                status_code=503 if exc.retryable else 400,
                retryable=exc.retryable,
            ) from exc
        except MultiTimeframeMarketDataError as exc:
            raise MarketAgentError(
                "market_unavailable",
                "Completed multi-timeframe market data is unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        except (RuntimeError, OSError, ValueError) as exc:
            raise MarketAgentError(
                "analysis_failed",
                "Market analysis could not be completed",
                status_code=503,
                retryable=True,
            ) from exc
        if event is None:
            raise MarketAgentError("analysis_cancelled", "Market analysis was cancelled", status_code=409)
        return event

    async def on_closed_kline(self, payload: dict[str, Any]) -> None:
        """Persist one validated 5m close when it matches the active agent scope."""
        if self.queue_store is None:
            return
        self._load_once()
        symbol = str(payload.get("symbol", "")).upper()
        network = str(payload.get("network", "")).lower()
        if (
            not self._state.get("desired_enabled")
            or symbol != self._state.get("symbol")
            or network != self._state.get("network")
            or payload.get("interval") != TRIGGER_INTERVAL
        ):
            return
        close_time = int(payload["close_time"])
        await asyncio.to_thread(
            self.queue_store.enqueue_market_job,
            network,
            symbol,
            f"5m:{close_time}",
            payload={"cutoff_ms": close_time, "closed_kline": dict(payload)},
            reasons=("candle_closed",),
            priority=10,
        )

    async def _run_queue(self, generation: int, *, immediate: bool = False) -> None:
        del immediate
        assert self.queue_store is not None
        await asyncio.to_thread(self.queue_store.initialize)
        await asyncio.to_thread(self.queue_store.recover_expired_leases)
        await self._publish_pending_events()
        while generation == self._generation and self._state["desired_enabled"]:
            self._reset_daily_usage_if_needed()
            if self._state["daily_usage_count"] >= self.daily_limit:
                self._state.update(state="paused_budget", paused_reason="daily_budget")
                self._save()
                await asyncio.sleep(min(60.0, self.idle_poll_seconds))
                continue
            job = await asyncio.to_thread(self._claim_fair_job)
            if job is None:
                await self._publish_pending_events()
                await asyncio.sleep(self.idle_poll_seconds)
                continue
            try:
                await self._process_queued_job(job, generation)
                self._state.update(
                    state="running", paused_reason=None, retry_attempt=0, retry_not_before=None
                )
                self._save()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._release_failed_job(job, exc)
            await self._publish_pending_events()

    def _claim_fair_job(self) -> MarketAgentJob | None:
        assert self.queue_store is not None
        scope = {
            "network": self._state["network"],
            "symbol": self._state["symbol"],
            "lease_ms": 180_000,
        }
        lanes = [JobLane.MARKET] if self._inbox_streak >= 2 else [JobLane.INBOX]
        job = self.queue_store.claim_next(self.worker_id, lanes=lanes, **scope)
        if job is None:
            other = [JobLane.INBOX] if lanes == [JobLane.MARKET] else [JobLane.MARKET]
            job = self.queue_store.claim_next(self.worker_id, lanes=other, **scope)
        if job is not None:
            self._inbox_streak = self._inbox_streak + 1 if job.lane is JobLane.INBOX else 0
        return job

    async def _process_queued_job(self, job: MarketAgentJob, generation: int) -> None:
        if generation != self._generation or not self._state["desired_enabled"]:
            raise asyncio.CancelledError
        client = self.client_getter()
        if client is None:
            raise MarketAgentError(
                "market_unavailable", "Binance market service is unavailable", retryable=True
            )
        config_id, provider_config, proxy_url = await asyncio.to_thread(
            self._resolve_config, None
        )
        self._state["config_id"] = config_id
        self._state["daily_usage_count"] += 1
        self._state["active_batch_id"] = job.id
        self._save()
        manual = job.lane is JobLane.INBOX

        async def on_batch_ready(batch_id: str, thread_id: str, cutoff: str) -> None:
            self._state.update(
                active_batch_id=job.id,
                active_thread_id=thread_id,
                last_scheduled_cutoff=cutoff,
            )
            self._save()

        history = []
        if manual:
            history = [
                item for item in self._state["summaries"] if item.get("role") == "assistant"
            ][-MAX_SUMMARIES:]
        heartbeat = asyncio.create_task(
            self._renew_job_lease(job.id), name=f"market-agent-lease-{job.id}"
        )
        try:
            result = await self.graph.run(
                symbol=job.symbol,
                mode="manual" if manual else "automatic",
                manual_query=job.payload.get("content") if manual else None,
                history=history,
                client=ReadOnlyMarketGateway(client),
                provider_config=provider_config,
                proxy_url=proxy_url,
                thread_id=job.id,
                cutoff_ms=job.payload.get("cutoff_ms"),
                on_batch_ready=on_batch_ready,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        five_minute = result["snapshot"]["intervals"]["5m"]
        structured = {
            **dict(result.get("structured") or {}),
            "bar_closed_at": result["snapshot"]["trigger_cutoff"],
            "close": five_minute["close"],
            "sar_direction": five_minute["sar"]["direction"],
            "adx": five_minute["adx"]["value"],
            "plus_di": five_minute["adx"]["plus_di"],
            "minus_di": five_minute["adx"]["minus_di"],
        }
        if manual:
            structured["client_message_id"] = job.payload.get("client_message_id")
        event = await asyncio.to_thread(
            self.queue_store.complete_job,
            job.id,
            self.worker_id,
            result={"batch_id": result["batch_id"], "structured": structured},
            event_type="assistant_message" if manual else "analysis",
            role="assistant",
            content=str(result["answer"])[:MAX_ANSWER_LENGTH],
            structured=structured,
            reasons=result["reasons"],
        )
        self._append_summary(dict(result["summary"]))
        self._state.update(
            active_batch_id=None,
            active_thread_id=None,
            last_committed_batch_id=job.id,
        )
        self._save()
        await self._publish_ledger_event(event)

    async def _renew_job_lease(self, job_id: str) -> None:
        assert self.queue_store is not None
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(
                self.queue_store.renew_lease,
                job_id,
                self.worker_id,
                lease_ms=180_000,
            )

    async def _release_failed_job(self, job: MarketAgentJob, exc: Exception) -> None:
        assert self.queue_store is not None
        retryable = not isinstance(exc, MarketAgentError) or exc.retryable
        code = getattr(exc, "code", "analysis_failed")
        try:
            if retryable and job.attempts < len(self.retry_delays):
                delay = self.retry_delays[min(job.attempts - 1, len(self.retry_delays) - 1)]
                await asyncio.to_thread(
                    self.queue_store.retry_job,
                    job.id,
                    self.worker_id,
                    available_at_ms=utc_ms() + int(delay * 1000),
                    error_code=code,
                )
                self._state.update(state="retry_wait", paused_reason=code)
            else:
                await asyncio.to_thread(
                    self.queue_store.fail_job,
                    job.id,
                    self.worker_id,
                    error_code=code,
                )
                self._state.update(state="paused_config", paused_reason=code)
            self._save()
        except Exception:
            logger.exception("Market agent failed to release queued job {}", job.id)

    async def _publish_pending_events(self) -> None:
        if self.queue_store is None:
            return
        events = await asyncio.to_thread(
            self.queue_store.events, after_sequence=0, limit=100, unpublished_only=True
        )
        for event in events:
            await self._publish_ledger_event(event)

    async def _publish_ledger_event(self, event: MarketAgentEvent) -> None:
        assert self.queue_store is not None
        payload = self._ledger_event_payload(event)
        await self.notifier({"type": "market_agent_event", "data": payload})
        await asyncio.to_thread(self.queue_store.mark_event_published, event.sequence)

    @staticmethod
    def _ledger_event_payload(event: MarketAgentEvent) -> dict[str, Any]:
        created_at = datetime.fromtimestamp(
            event.created_at_ms / 1000, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return {
            "sequence": event.sequence,
            "job_id": event.job_id,
            "batch_id": event.job_id,
            "type": event.event_type,
            "event_type": event.event_type,
            "role": event.role,
            "content": event.content,
            "answer": event.content,
            "network": event.network,
            "symbol": event.symbol,
            "reasons": list(event.reasons),
            "context": dict(event.structured),
            "structured": dict(event.structured),
            "bar_closed_at": event.structured.get("bar_closed_at"),
            "created_at": created_at,
        }

    def _resolve_config(self, expected_id: int | None) -> tuple[int, dict[str, Any], str | None]:
        session = None
        try:
            session = self.session_factory()
            config = session.query(AIConfig).filter(AIConfig.is_active.is_(True)).first()
            if config is None or (expected_id is not None and config.id != expected_id):
                raise MarketAgentError("config_unavailable", "Active AI configuration is unavailable")
            api_key = decrypt(config.api_key_enc) if config.api_key_enc else ""
            validated = validate_ai_config(
                name=config.name,
                provider=config.provider,
                api_key=None,
                base_url=config.base_url,
                model_name=config.model_name,
                existing_api_key=api_key,
            )
            settings = session.query(Settings).first()
            return config.id, validated.as_provider_config(), settings.proxy_url if settings else None
        except (AIConfigValidationError, InvalidToken) as exc:
            raise MarketAgentError("config_unavailable", "Active AI configuration is unavailable") from exc
        except MarketAgentError:
            raise
        except Exception as exc:
            logger.warning(
                "Market agent AI configuration lookup failed: exception_type={}",
                type(exc).__name__,
            )
            raise MarketAgentError(
                "config_lookup_failed",
                "Active AI configuration could not be loaded",
                status_code=503,
                retryable=True,
            ) from exc
        finally:
            if session is not None:
                session.close()

    def _reset_daily_usage_if_needed(self) -> None:
        today = _utc_now().date().isoformat()
        if self._state["daily_usage_date"] != today:
            self._state["daily_usage_date"] = today
            self._state["daily_usage_count"] = 0

    def _next_trigger_delay(self) -> float:
        now = _utc_now().timestamp()
        next_boundary = (int(now) // TRIGGER_SECONDS + 1) * TRIGGER_SECONDS + 2
        return max(self.idle_poll_seconds, next_boundary - now)

    async def _run(self, generation: int, *, immediate: bool = False) -> None:
        if not immediate:
            await asyncio.sleep(self._next_trigger_delay())
        while generation == self._generation and self._state["desired_enabled"]:
            client = self.client_getter()
            if client is None:
                self._state.update(state="waiting_market", paused_reason="market_unavailable")
                self._save()
                await asyncio.sleep(self.idle_poll_seconds)
                continue

            self._reset_daily_usage_if_needed()
            if self._state["daily_usage_count"] >= self.daily_limit:
                self._state.update(state="paused_budget", paused_reason="daily_budget")
                self._save()
                await asyncio.sleep(min(60.0, max(self.idle_poll_seconds, 1.0)))
                continue

            try:
                await self._process_batch(client, generation, mode="automatic")
                self._state.update(
                    state="running",
                    paused_reason=None,
                    retry_attempt=0,
                    retry_not_before=None,
                )
                self._save()
                await asyncio.sleep(self._next_trigger_delay())
            except asyncio.CancelledError:
                raise
            except MarketAgentError as exc:
                if not exc.retryable:
                    self._state.update(state="paused_config", paused_reason=exc.code)
                    self._save()
                    await asyncio.sleep(min(60.0, max(self.idle_poll_seconds, 1.0)))
                    continue
                await self._record_transient_failure(exc)
            except AIProviderError as exc:
                if not exc.retryable:
                    self._state.update(state="paused_config", paused_reason=exc.code)
                    self._save()
                    await asyncio.sleep(min(60.0, max(self.idle_poll_seconds, 1.0)))
                    continue
                await self._record_transient_failure(exc)
            except (MultiTimeframeMarketDataError, RuntimeError, OSError) as exc:
                await self._record_transient_failure(exc)
            except Exception as exc:
                await self._record_transient_failure(exc)

    async def _record_transient_failure(self, exc: Exception) -> None:
        attempt = int(self._state["retry_attempt"]) + 1
        delay = float(self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)])
        retry_at = datetime.fromtimestamp(_utc_now().timestamp() + delay, tz=timezone.utc)
        self._state.update(
            state="retry_wait",
            paused_reason="transient_failure",
            retry_attempt=attempt,
            retry_not_before=_iso_now(retry_at),
        )
        self._save()
        logger.warning(
            "Market agent batch failed: agent_id={} attempt={} retry_seconds={} exception_type={}",
            self._state["agent_id"],
            attempt,
            delay,
            type(exc).__name__,
        )
        await asyncio.sleep(delay)

    async def _process_batch(
        self,
        client: Any,
        generation: int,
        *,
        mode: str,
        manual_query: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._analysis_lock:
            return await self._process_batch_serialized(
                client,
                generation,
                mode=mode,
                manual_query=manual_query,
            )

    async def _process_batch_serialized(
        self,
        client: Any,
        generation: int,
        *,
        mode: str,
        manual_query: str | None = None,
    ) -> dict[str, Any] | None:
        if client is None:
            raise MarketAgentError(
                "market_unavailable", "Binance market service is unavailable", retryable=True
            )
        config_id, provider_config, proxy_url = await asyncio.to_thread(
            self._resolve_config, None
        )
        self._state["config_id"] = config_id
        self._reset_daily_usage_if_needed()
        if self._state["daily_usage_count"] >= self.daily_limit:
            raise MarketAgentError("daily_budget", "Daily AI analysis budget is exhausted")

        thread_id = str(uuid4())

        async def on_batch_ready(batch_id: str, active_thread_id: str, cutoff: str) -> None:
            if generation != self._generation or not self._state["desired_enabled"]:
                raise asyncio.CancelledError
            self._reset_daily_usage_if_needed()
            if self._state["daily_usage_count"] >= self.daily_limit:
                raise MarketAgentError("daily_budget", "Daily AI analysis budget is exhausted")
            self._state.update(
                active_batch_id=batch_id,
                active_thread_id=active_thread_id,
                last_scheduled_cutoff=cutoff,
            )
            self._state["daily_usage_count"] += 1
            self._save()

        result = await self.graph.run(
            symbol=self._state["symbol"],
            mode=mode,
            manual_query=manual_query,
            history=list(self._state["summaries"])[-MAX_SUMMARIES:],
            client=client,
            provider_config=provider_config,
            proxy_url=proxy_url,
            thread_id=thread_id,
            on_batch_ready=on_batch_ready,
        )
        if generation != self._generation or not self._state["desired_enabled"]:
            return None

        batch_id = result["batch_id"]
        duplicate = any(event.get("batch_id") == batch_id for event in self._state["events"])
        self._state.update(
            last_scheduled_cutoff=result["snapshot"]["trigger_cutoff"],
            last_committed_batch_id=batch_id,
            active_batch_id=None,
            active_thread_id=None,
        )
        if duplicate:
            self._save()
            return None

        five_minute = result["snapshot"]["intervals"]["5m"]
        event = self._append_event(
            event_type="assistant_message" if mode == "manual" else "analysis",
            role="assistant",
            content=str(result["answer"])[:MAX_ANSWER_LENGTH],
            batch_id=batch_id,
            cutoff=result["snapshot"]["trigger_cutoff"],
            reasons=list(result["reasons"]),
            context={
                "close": five_minute["close"],
                "sar_direction": five_minute["sar"]["direction"],
                "adx": five_minute["adx"]["value"],
                "plus_di": five_minute["adx"]["plus_di"],
                "minus_di": five_minute["adx"]["minus_di"],
                "model_name": provider_config["model_name"],
                "analysis_intervals": list(ANALYSIS_INTERVALS),
            },
        )
        self._append_summary(dict(result["summary"]))
        self._state.update(retry_attempt=0, retry_not_before=None)
        self._save()
        await self._notify_event(event)
        return event

    def _append_event(
        self,
        *,
        event_type: str,
        role: str,
        content: str,
        batch_id: str,
        cutoff: str | None,
        reasons: list[str],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        sequence = int(self._state["next_sequence"])
        event = {
            "sequence": sequence,
            "type": event_type,
            "role": role,
            "content": content,
            "answer": content if role == "assistant" else None,
            "agent_id": self._state["agent_id"],
            "symbol": self._state["symbol"],
            "batch_id": batch_id,
            "bar_closed_at": cutoff,
            "created_at": _iso_now(),
            "reasons": reasons,
            "context": context,
        }
        self._state["events"] = [*self._state["events"], event][-MAX_EVENTS:]
        self._state["next_sequence"] = sequence + 1
        return event

    def _append_summary(self, summary: dict[str, Any]) -> None:
        safe = {
            "role": summary.get("role") if summary.get("role") in {"user", "assistant"} else "assistant",
            "content": str(summary.get("content", ""))[:600],
            "batch_id": summary.get("batch_id"),
            "cutoff": summary.get("cutoff"),
            "close": summary.get("close"),
            "sar_direction": summary.get("sar_direction"),
            "adx": summary.get("adx"),
        }
        self._state["summaries"] = [*self._state["summaries"], safe][-MAX_SUMMARIES:]

    async def _notify_event(self, event: dict[str, Any]) -> None:
        await self.notifier(
            {
                "type": "market_agent_event",
                "data": {
                    "agent_id": event["agent_id"],
                    "sequence": event["sequence"],
                    "event_type": event["type"],
                    "symbol": event["symbol"],
                    "batch_id": event["batch_id"],
                    "bar_closed_at": event["bar_closed_at"],
                    "reasons": event["reasons"],
                },
            }
        )


market_agent_manager = MarketAgentManager(queue_store=MarketAgentQueueStore())
