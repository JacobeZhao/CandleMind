"""Transactional SQLite inbox, outbox, and task ledger for the market agent."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any
from uuid import uuid4

from backend.app.runtime_paths import RUNTIME_DATA_DIR

from .market_agent_contracts import (
    JobLane,
    JobState,
    MarketAgentEvent,
    MarketAgentJob,
    MarketAgentLedgerConflict,
    MarketAgentLedgerError,
    MarketAgentLeaseError,
    bounded_text,
    normalize_scope,
)


SCHEMA_VERSION = 1
DEFAULT_PATH = RUNTIME_DATA_DIR / "agents" / "market_agent_harness.sqlite3"
_JOB_STATES = tuple(state.value for state in JobState)
_JOB_LANES = tuple(lane.value for lane in JobLane)


def utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _json_object(value: Mapping[str, Any] | None, *, name: str) -> str:
    document = dict(value or {})
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite JSON object") from exc


def _reasons(value: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for item in value or ():
        reason = bounded_text(item, name="reason", maximum=64)
        if reason not in result:
            result.append(reason)
    if len(result) > 16:
        raise ValueError("reasons cannot contain more than 16 values")
    return tuple(result)


class MarketAgentQueueStore:
    """Own durable work and publication state without performing external calls."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or DEFAULT_PATH).resolve()
        self._lock = RLock()
        self._initialized = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._connection() as connection:
                    try:
                        connection.executescript(
                            f"""
                            BEGIN IMMEDIATE;
                            CREATE TABLE IF NOT EXISTS schema_meta (
                                key TEXT PRIMARY KEY,
                                value TEXT NOT NULL
                            );
                            CREATE TABLE IF NOT EXISTS jobs (
                                id TEXT PRIMARY KEY,
                                network TEXT NOT NULL CHECK(network IN ('testnet','mainnet')),
                                symbol TEXT NOT NULL,
                                lane TEXT NOT NULL CHECK(lane IN {_JOB_LANES}),
                                dedupe_key TEXT NOT NULL,
                                state TEXT NOT NULL CHECK(state IN {_JOB_STATES}),
                                priority INTEGER NOT NULL DEFAULT 0,
                                payload_json TEXT NOT NULL,
                                reasons_json TEXT NOT NULL,
                                result_json TEXT,
                                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                                available_at_ms INTEGER NOT NULL,
                                lease_owner TEXT,
                                lease_expires_at_ms INTEGER,
                                error_code TEXT,
                                created_at_ms INTEGER NOT NULL,
                                updated_at_ms INTEGER NOT NULL,
                                completed_at_ms INTEGER,
                                UNIQUE(network, symbol, dedupe_key),
                                CHECK((state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at_ms IS NOT NULL)
                                   OR (state != 'running' AND lease_owner IS NULL AND lease_expires_at_ms IS NULL))
                            );
                            CREATE INDEX IF NOT EXISTS jobs_claimable
                                ON jobs(state, available_at_ms, priority DESC, created_at_ms, id);
                            CREATE INDEX IF NOT EXISTS jobs_scope
                                ON jobs(network, symbol, state, created_at_ms);
                            CREATE TABLE IF NOT EXISTS inbox_messages (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                network TEXT NOT NULL,
                                symbol TEXT NOT NULL,
                                client_message_id TEXT NOT NULL,
                                content TEXT NOT NULL,
                                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                                created_at_ms INTEGER NOT NULL,
                                UNIQUE(network, symbol, client_message_id)
                            );
                            CREATE TABLE IF NOT EXISTS outbox_events (
                                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
                                network TEXT NOT NULL,
                                symbol TEXT NOT NULL,
                                event_type TEXT NOT NULL,
                                role TEXT NOT NULL,
                                content TEXT NOT NULL,
                                structured_json TEXT NOT NULL,
                                reasons_json TEXT NOT NULL,
                                created_at_ms INTEGER NOT NULL,
                                published_at_ms INTEGER
                            );
                            CREATE INDEX IF NOT EXISTS outbox_unpublished
                                ON outbox_events(published_at_ms, sequence);
                            """
                        )
                        row = connection.execute(
                            "SELECT value FROM schema_meta WHERE key='schema_version'"
                        ).fetchone()
                        if row is not None and int(row["value"]) != SCHEMA_VERSION:
                            raise MarketAgentLedgerError(
                                "market-agent ledger schema is incompatible"
                            )
                        connection.execute(
                            "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
            except MarketAgentLedgerError:
                raise
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise MarketAgentLedgerError(
                    "market-agent ledger initialization failed"
                ) from exc
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def enqueue_market_job(
        self,
        network: str,
        symbol: str,
        dedupe_key: str,
        *,
        payload: Mapping[str, Any] | None = None,
        reasons: Sequence[str] = (),
        priority: int = 0,
        available_at_ms: int | None = None,
        job_id: str | None = None,
        now_ms: int | None = None,
    ) -> MarketAgentJob:
        return self._enqueue_job(
            network,
            symbol,
            JobLane.MARKET,
            dedupe_key,
            payload=payload,
            reasons=reasons,
            priority=priority,
            available_at_ms=available_at_ms,
            job_id=job_id,
            now_ms=now_ms,
        )

    def enqueue_inbox_message(
        self,
        network: str,
        symbol: str,
        client_message_id: str,
        content: str,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int = 0,
        available_at_ms: int | None = None,
        job_id: str | None = None,
        now_ms: int | None = None,
    ) -> MarketAgentJob:
        network, symbol = normalize_scope(network, symbol)
        message_id = bounded_text(
            client_message_id, name="client_message_id", maximum=128
        )
        message = bounded_text(content, name="content", maximum=1_000)
        created = utc_ms() if now_ms is None else int(now_ms)
        merged_payload = dict(payload or {})
        merged_payload["client_message_id"] = message_id
        merged_payload["content"] = message
        dedupe_key = f"inbox:{message_id}"
        encoded_payload = _json_object(merged_payload, name="payload")
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT j.*, i.content AS inbox_content
                   FROM inbox_messages i JOIN jobs j ON j.id=i.job_id
                   WHERE i.network=? AND i.symbol=? AND i.client_message_id=?""",
                (network, symbol, message_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["inbox_content"] != message
                    or existing["payload_json"] != encoded_payload
                ):
                    raise MarketAgentLedgerConflict(
                        "client_message_id was already used with different content"
                    )
                return self._job(existing)
            job = self._insert_job(
                connection,
                network,
                symbol,
                JobLane.INBOX,
                dedupe_key,
                payload_json=encoded_payload,
                reasons=(),
                priority=priority,
                available_at_ms=available_at_ms,
                job_id=job_id,
                now_ms=created,
            )
            connection.execute(
                """INSERT INTO inbox_messages(
                       network,symbol,client_message_id,content,job_id,created_at_ms
                   ) VALUES(?,?,?,?,?,?)""",
                (network, symbol, message_id, message, job.id, created),
            )
            return job

    def _enqueue_job(
        self,
        network: str,
        symbol: str,
        lane: JobLane,
        dedupe_key: str,
        *,
        payload: Mapping[str, Any] | None,
        reasons: Sequence[str],
        priority: int,
        available_at_ms: int | None,
        job_id: str | None,
        now_ms: int | None,
    ) -> MarketAgentJob:
        network, symbol = normalize_scope(network, symbol)
        key = bounded_text(dedupe_key, name="dedupe_key", maximum=256)
        encoded_payload = _json_object(payload, name="payload")
        normalized_reasons = _reasons(reasons)
        created = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE network=? AND symbol=? AND dedupe_key=?",
                (network, symbol, key),
            ).fetchone()
            if existing is not None:
                if existing["lane"] != lane.value or existing["payload_json"] != encoded_payload:
                    raise MarketAgentLedgerConflict(
                        "dedupe_key was already used with different input"
                    )
                if existing["state"] in {
                    JobState.PENDING.value,
                    JobState.RETRY_WAIT.value,
                }:
                    merged = _reasons(
                        (*self._decode_reasons(existing["reasons_json"]), *normalized_reasons)
                    )
                    next_priority = max(int(existing["priority"]), int(priority))
                    connection.execute(
                        "UPDATE jobs SET reasons_json=?,priority=?,updated_at_ms=? WHERE id=?",
                        (
                            json.dumps(merged, separators=(",", ":")),
                            next_priority,
                            created,
                            existing["id"],
                        ),
                    )
                return self._job(
                    connection.execute(
                        "SELECT * FROM jobs WHERE id=?", (existing["id"],)
                    ).fetchone()
                )
            return self._insert_job(
                connection,
                network,
                symbol,
                lane,
                key,
                payload_json=encoded_payload,
                reasons=normalized_reasons,
                priority=priority,
                available_at_ms=available_at_ms,
                job_id=job_id,
                now_ms=created,
            )

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        network: str,
        symbol: str,
        lane: JobLane,
        dedupe_key: str,
        *,
        payload_json: str,
        reasons: Sequence[str],
        priority: int,
        available_at_ms: int | None,
        job_id: str | None,
        now_ms: int,
    ) -> MarketAgentJob:
        identifier = bounded_text(job_id or str(uuid4()), name="job_id", maximum=128)
        available = now_ms if available_at_ms is None else int(available_at_ms)
        connection.execute(
            """INSERT INTO jobs(
                   id,network,symbol,lane,dedupe_key,state,priority,payload_json,
                   reasons_json,attempts,available_at_ms,created_at_ms,updated_at_ms
               ) VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,?)""",
            (
                identifier,
                network,
                symbol,
                lane.value,
                dedupe_key,
                int(priority),
                payload_json,
                json.dumps(tuple(reasons), separators=(",", ":")),
                0,
                available,
                now_ms,
                now_ms,
            ),
        )
        return self._job(
            connection.execute(
                "SELECT * FROM jobs WHERE id=?", (identifier,)
            ).fetchone()
        )

    def recover_expired_leases(self, *, now_ms: int | None = None) -> int:
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET state='retry_wait',available_at_ms=?,lease_owner=NULL,
                       lease_expires_at_ms=NULL,error_code='lease_expired',updated_at_ms=?
                   WHERE state='running' AND lease_expires_at_ms<=?""",
                (now, now, now),
            )
            return cursor.rowcount

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_ms: int,
        lanes: Sequence[JobLane | str] | None = None,
        network: str | None = None,
        symbol: str | None = None,
        now_ms: int | None = None,
    ) -> MarketAgentJob | None:
        owner = bounded_text(worker_id, name="worker_id", maximum=128)
        if int(lease_ms) <= 0:
            raise ValueError("lease_ms must be positive")
        now = utc_ms() if now_ms is None else int(now_ms)
        normalized_lanes = tuple(JobLane(item).value for item in (lanes or tuple(JobLane)))
        if not normalized_lanes:
            raise ValueError("lanes cannot be empty")
        scope_parameters: list[Any] = []
        scope_sql = ""
        if network is not None or symbol is not None:
            if network is None or symbol is None:
                raise ValueError("network and symbol must be provided together")
            normalized_network, normalized_symbol = normalize_scope(network, symbol)
            scope_sql = " AND network=? AND symbol=?"
            scope_parameters.extend((normalized_network, normalized_symbol))
        placeholders = ",".join("?" for _ in normalized_lanes)
        with self.transaction() as connection:
            connection.execute(
                """UPDATE jobs SET state='retry_wait',available_at_ms=?,lease_owner=NULL,
                       lease_expires_at_ms=NULL,error_code='lease_expired',updated_at_ms=?
                   WHERE state='running' AND lease_expires_at_ms<=?""",
                (now, now, now),
            )
            row = connection.execute(
                f"""SELECT id FROM jobs
                    WHERE state IN ('pending','retry_wait') AND available_at_ms<=?
                      AND lane IN ({placeholders}){scope_sql}
                    ORDER BY priority DESC, available_at_ms, created_at_ms, id
                    LIMIT 1""",
                (now, *normalized_lanes, *scope_parameters),
            ).fetchone()
            if row is None:
                return None
            expires = now + int(lease_ms)
            cursor = connection.execute(
                """UPDATE jobs SET state='running',attempts=attempts+1,lease_owner=?,
                       lease_expires_at_ms=?,error_code=NULL,updated_at_ms=?
                   WHERE id=? AND state IN ('pending','retry_wait')""",
                (owner, expires, now, row["id"]),
            )
            if cursor.rowcount != 1:
                raise MarketAgentLedgerError("claim lost its transactional candidate")
            return self._job(
                connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_ms: int,
        now_ms: int | None = None,
    ) -> MarketAgentJob:
        identifier = bounded_text(job_id, name="job_id", maximum=128)
        owner = bounded_text(worker_id, name="worker_id", maximum=128)
        if int(lease_ms) <= 0:
            raise ValueError("lease_ms must be positive")
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            self._require_running_lease(connection, identifier, owner, now_ms=now)
            connection.execute(
                "UPDATE jobs SET lease_expires_at_ms=?,updated_at_ms=? WHERE id=?",
                (now + int(lease_ms), now, identifier),
            )
            return self._job(
                connection.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            )

    def retry_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        available_at_ms: int,
        error_code: str,
        now_ms: int | None = None,
    ) -> MarketAgentJob:
        return self._release_running_job(
            job_id,
            worker_id,
            state=JobState.RETRY_WAIT,
            available_at_ms=int(available_at_ms),
            error_code=error_code,
            now_ms=now_ms,
        )

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        now_ms: int | None = None,
    ) -> MarketAgentJob:
        return self._release_running_job(
            job_id,
            worker_id,
            state=JobState.FAILED,
            available_at_ms=None,
            error_code=error_code,
            now_ms=now_ms,
        )

    def _release_running_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        state: JobState,
        available_at_ms: int | None,
        error_code: str,
        now_ms: int | None,
    ) -> MarketAgentJob:
        identifier = bounded_text(job_id, name="job_id", maximum=128)
        owner = bounded_text(worker_id, name="worker_id", maximum=128)
        code = bounded_text(error_code, name="error_code", maximum=128)
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            row = self._require_running_lease(connection, identifier, owner, now_ms=now)
            available = int(row["available_at_ms"]) if available_at_ms is None else available_at_ms
            completed = now if state is JobState.FAILED else None
            connection.execute(
                """UPDATE jobs SET state=?,available_at_ms=?,lease_owner=NULL,
                       lease_expires_at_ms=NULL,error_code=?,updated_at_ms=?,completed_at_ms=?
                   WHERE id=?""",
                (state.value, available, code, now, completed, identifier),
            )
            return self._job(
                connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (identifier,)
                ).fetchone()
            )

    def supersede_job(
        self, job_id: str, *, reason: str = "superseded", now_ms: int | None = None
    ) -> MarketAgentJob:
        identifier = bounded_text(job_id, name="job_id", maximum=128)
        code = bounded_text(reason, name="reason", maximum=128)
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if row["state"] == JobState.SUPERSEDED.value:
                return self._job(row)
            if row["state"] not in {JobState.PENDING.value, JobState.RETRY_WAIT.value}:
                raise MarketAgentLedgerConflict("only queued jobs can be superseded")
            connection.execute(
                """UPDATE jobs SET state='superseded',error_code=?,updated_at_ms=?,
                       completed_at_ms=? WHERE id=?""",
                (code, now, now, identifier),
            )
            return self._job(
                connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (identifier,)
                ).fetchone()
            )

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: Mapping[str, Any],
        event_type: str,
        role: str,
        content: str,
        structured: Mapping[str, Any] | None = None,
        reasons: Sequence[str] | None = None,
        now_ms: int | None = None,
    ) -> MarketAgentEvent:
        identifier = bounded_text(job_id, name="job_id", maximum=128)
        owner = bounded_text(worker_id, name="worker_id", maximum=128)
        normalized_event_type = bounded_text(event_type, name="event_type", maximum=64)
        normalized_role = bounded_text(role, name="role", maximum=32)
        normalized_content = bounded_text(content, name="content", maximum=8_000)
        result_json = _json_object(result, name="result")
        structured_json = _json_object(structured, name="structured")
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            existing_event = connection.execute(
                "SELECT * FROM outbox_events WHERE job_id=?", (identifier,)
            ).fetchone()
            if existing_event is not None:
                return self._event(existing_event)
            job = self._require_running_lease(connection, identifier, owner, now_ms=now)
            event_reasons = _reasons(
                reasons if reasons is not None else self._decode_reasons(job["reasons_json"])
            )
            connection.execute(
                """UPDATE jobs SET state='completed',result_json=?,lease_owner=NULL,
                       lease_expires_at_ms=NULL,error_code=NULL,updated_at_ms=?,completed_at_ms=?
                   WHERE id=?""",
                (result_json, now, now, identifier),
            )
            connection.execute(
                """INSERT INTO outbox_events(
                       job_id,network,symbol,event_type,role,content,structured_json,
                       reasons_json,created_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    job["network"],
                    job["symbol"],
                    normalized_event_type,
                    normalized_role,
                    normalized_content,
                    structured_json,
                    json.dumps(event_reasons, separators=(",", ":")),
                    now,
                ),
            )
            return self._event(
                connection.execute(
                    "SELECT * FROM outbox_events WHERE job_id=?", (identifier,)
                ).fetchone()
            )

    def mark_event_published(
        self, sequence: int, *, now_ms: int | None = None
    ) -> MarketAgentEvent:
        now = utc_ms() if now_ms is None else int(now_ms)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM outbox_events WHERE sequence=?", (int(sequence),)
            ).fetchone()
            if row is None:
                raise KeyError(sequence)
            published = int(row["published_at_ms"]) if row["published_at_ms"] is not None else now
            connection.execute(
                "UPDATE outbox_events SET published_at_ms=? WHERE sequence=?",
                (published, int(sequence)),
            )
            connection.execute(
                """UPDATE jobs SET state='published',updated_at_ms=?
                   WHERE id=? AND state IN ('completed','published')""",
                (published, row["job_id"]),
            )
            return self._event(
                connection.execute(
                    "SELECT * FROM outbox_events WHERE sequence=?", (int(sequence),)
                ).fetchone()
            )

    def get_job(self, job_id: str) -> MarketAgentJob | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone()
            return None if row is None else self._job(row)

    def events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        unpublished_only: bool = False,
        network: str | None = None,
        symbol: str | None = None,
    ) -> list[MarketAgentEvent]:
        if not 1 <= int(limit) <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        predicates = ["sequence>?"]
        parameters: list[Any] = [int(after_sequence)]
        if unpublished_only:
            predicates.append("published_at_ms IS NULL")
        if network is not None or symbol is not None:
            if network is None or symbol is None:
                raise ValueError("network and symbol must be provided together")
            normalized_network, normalized_symbol = normalize_scope(network, symbol)
            predicates.extend(("network=?", "symbol=?"))
            parameters.extend((normalized_network, normalized_symbol))
        parameters.append(int(limit))
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM outbox_events WHERE {' AND '.join(predicates)} "
                "ORDER BY sequence LIMIT ?",
                parameters,
            ).fetchall()
            return [self._event(row) for row in rows]

    def status_summary(self, network: str, symbol: str) -> dict[str, Any]:
        network, symbol = normalize_scope(network, symbol)
        self.initialize()
        with self._connection() as connection:
            counts = {state.value: 0 for state in JobState}
            for row in connection.execute(
                """SELECT state,COUNT(*) AS count FROM jobs
                   WHERE network=? AND symbol=? GROUP BY state""",
                (network, symbol),
            ):
                counts[row["state"]] = int(row["count"])
            lane_counts = {lane.value: 0 for lane in JobLane}
            for row in connection.execute(
                """SELECT lane,COUNT(*) AS count FROM jobs
                   WHERE network=? AND symbol=?
                     AND state IN ('pending','running','retry_wait') GROUP BY lane""",
                (network, symbol),
            ):
                lane_counts[row["lane"]] = int(row["count"])
            latest = connection.execute(
                """SELECT COALESCE(MAX(sequence),0) AS latest_sequence,
                          SUM(CASE WHEN published_at_ms IS NULL THEN 1 ELSE 0 END) AS unpublished
                   FROM outbox_events WHERE network=? AND symbol=?""",
                (network, symbol),
            ).fetchone()
            return {
                "network": network,
                "symbol": symbol,
                "states": counts,
                "lanes": lane_counts,
                "queue_depth": sum(
                    counts[state.value]
                    for state in (JobState.PENDING, JobState.RUNNING, JobState.RETRY_WAIT)
                ),
                "latest_sequence": int(latest["latest_sequence"]),
                "unpublished_events": int(latest["unpublished"] or 0),
            }

    @staticmethod
    def _require_running_lease(
        connection: sqlite3.Connection,
        job_id: str,
        worker_id: str,
        *,
        now_ms: int,
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["state"] != JobState.RUNNING.value or row["lease_owner"] != worker_id:
            raise MarketAgentLeaseError("worker does not own the running job lease")
        if int(row["lease_expires_at_ms"]) <= now_ms:
            raise MarketAgentLeaseError("running job lease has expired")
        return row

    @staticmethod
    def _decode_reasons(encoded: str) -> tuple[str, ...]:
        return tuple(str(value) for value in json.loads(encoded))

    @classmethod
    def _job(cls, row: sqlite3.Row) -> MarketAgentJob:
        return MarketAgentJob(
            id=row["id"],
            network=row["network"],
            symbol=row["symbol"],
            lane=JobLane(row["lane"]),
            dedupe_key=row["dedupe_key"],
            state=JobState(row["state"]),
            priority=int(row["priority"]),
            payload=dict(json.loads(row["payload_json"])),
            reasons=cls._decode_reasons(row["reasons_json"]),
            result=None if row["result_json"] is None else dict(json.loads(row["result_json"])),
            attempts=int(row["attempts"]),
            available_at_ms=int(row["available_at_ms"]),
            lease_owner=row["lease_owner"],
            lease_expires_at_ms=(
                None if row["lease_expires_at_ms"] is None else int(row["lease_expires_at_ms"])
            ),
            error_code=row["error_code"],
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            completed_at_ms=(
                None if row["completed_at_ms"] is None else int(row["completed_at_ms"])
            ),
        )

    @classmethod
    def _event(cls, row: sqlite3.Row) -> MarketAgentEvent:
        return MarketAgentEvent(
            sequence=int(row["sequence"]),
            job_id=row["job_id"],
            network=row["network"],
            symbol=row["symbol"],
            event_type=row["event_type"],
            role=row["role"],
            content=row["content"],
            structured=dict(json.loads(row["structured_json"])),
            reasons=cls._decode_reasons(row["reasons_json"]),
            created_at_ms=int(row["created_at_ms"]),
            published_at_ms=(
                None if row["published_at_ms"] is None else int(row["published_at_ms"])
            ),
        )


__all__ = ["MarketAgentQueueStore", "SCHEMA_VERSION", "utc_ms"]
