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
from .multi_timeframe_market_snapshot import MultiTimeframeMarketDataError


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
        return {
            "agent_id": state["agent_id"],
            "state": state["state"],
            "desired_enabled": bool(state["desired_enabled"]),
            "enabled": bool(state["desired_enabled"]),
            "symbol": state["symbol"],
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

    def events(self, *, after_sequence: int = 0, limit: int = MAX_EVENTS) -> list[dict[str, Any]]:
        self._load_once()
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
            self._state.update(
                desired_enabled=True,
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
            self._task = asyncio.create_task(
                self._run(generation, immediate=stopped_to_started),
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
        async with self._lock:
            self._load_once()
            if not self._state["desired_enabled"] or not self._state["symbol"]:
                self._state["state"] = "stopped"
                self._save()
                return self.status()
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
            self._task = asyncio.create_task(
                self._run(generation, immediate=False),
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

    async def message(self, *, symbol: str, content: str) -> dict[str, Any]:
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


market_agent_manager = MarketAgentManager()
