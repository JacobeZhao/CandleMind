"""Versioned SQLite persistence for strategy-owned exchange analytics."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from backend.app.runtime_paths import RUNTIME_DATA_DIR


SCHEMA_VERSION = 1
NETWORKS = {"testnet", "mainnet"}


class StrategyAnalyticsStoreError(RuntimeError):
    pass


def utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def decimal_string(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("monetary value must be a decimal") from exc
    if not number.is_finite():
        raise ValueError("monetary value must be finite")
    return format(number, "f")


class StrategyAnalyticsStore:
    """Keep analytics isolated from the operational application database."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or RUNTIME_DATA_DIR / "analytics" / "strategy_analytics.sqlite3").resolve()
        self._lock = RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scopes (
                        id INTEGER PRIMARY KEY,
                        account_fingerprint TEXT NOT NULL,
                        network TEXT NOT NULL CHECK(network IN ('testnet','mainnet')),
                        symbol TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        UNIQUE(account_fingerprint, network, symbol)
                    );
                    CREATE TABLE IF NOT EXISTS strategy_runs (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        run_id TEXT NOT NULL,
                        strategy_type TEXT NOT NULL,
                        config_version TEXT NOT NULL,
                        allocation_equity TEXT NOT NULL,
                        started_at_ms INTEGER NOT NULL,
                        ended_at_ms INTEGER,
                        PRIMARY KEY(scope_id, run_id)
                    );
                    CREATE TABLE IF NOT EXISTS owned_orders (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        run_id TEXT NOT NULL,
                        exchange_order_id TEXT,
                        client_order_id TEXT,
                        decision_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        captured_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(scope_id, run_id, decision_id, ordinal),
                        FOREIGN KEY(scope_id, run_id) REFERENCES strategy_runs(scope_id, run_id) ON DELETE CASCADE
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS owned_exchange_order
                        ON owned_orders(scope_id, exchange_order_id) WHERE exchange_order_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS owned_client_order
                        ON owned_orders(scope_id, client_order_id) WHERE client_order_id IS NOT NULL;
                    CREATE TABLE IF NOT EXISTS exchange_fills (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        exchange_trade_id TEXT NOT NULL,
                        exchange_order_id TEXT NOT NULL,
                        client_order_id TEXT,
                        time_ms INTEGER NOT NULL,
                        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                        quantity TEXT NOT NULL,
                        price TEXT NOT NULL,
                        realized_pnl TEXT NOT NULL,
                        commission TEXT NOT NULL,
                        commission_asset TEXT,
                        PRIMARY KEY(scope_id, exchange_trade_id)
                    );
                    CREATE TABLE IF NOT EXISTS income_events (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        transaction_id TEXT NOT NULL,
                        time_ms INTEGER NOT NULL,
                        income_type TEXT NOT NULL,
                        asset TEXT,
                        amount TEXT NOT NULL,
                        PRIMARY KEY(scope_id, transaction_id, income_type)
                    );
                    CREATE TABLE IF NOT EXISTS equity_inputs (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        time_ms INTEGER NOT NULL,
                        equity TEXT NOT NULL,
                        capital_flow TEXT NOT NULL DEFAULT '0',
                        mark_price TEXT,
                        PRIMARY KEY(scope_id, time_ms)
                    );
                    CREATE TABLE IF NOT EXISTS sync_state (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        stream TEXT NOT NULL,
                        cursor TEXT,
                        coverage_start_ms INTEGER,
                        coverage_end_ms INTEGER,
                        complete INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        reason TEXT,
                        updated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(scope_id, stream)
                    );
                    CREATE TABLE IF NOT EXISTS scope_integrity (
                        scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                        flag TEXT NOT NULL,
                        detected_at_ms INTEGER NOT NULL,
                        PRIMARY KEY(scope_id, flag)
                    );
                    """
                )
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is not None and int(row[0]) != SCHEMA_VERSION:
                    raise StrategyAnalyticsStoreError("analytics schema is incompatible")
                connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO scope_integrity(scope_id,flag,detected_at_ms)
                       SELECT scope_id,'external_fills_present',updated_at_ms
                       FROM sync_state WHERE reason='external_fills_present'"""
                )
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise StrategyAnalyticsStoreError("analytics database initialization failed") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

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

    def ensure_scope(self, account_fingerprint: str, network: str, symbol: str) -> int:
        if not account_fingerprint or len(account_fingerprint) > 128:
            raise ValueError("invalid account fingerprint")
        if network not in NETWORKS:
            raise ValueError("invalid analytics network")
        symbol = symbol.strip().upper()
        if not symbol.isalnum():
            raise ValueError("invalid analytics symbol")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO scopes(account_fingerprint,network,symbol,created_at_ms) VALUES(?,?,?,?)",
                (account_fingerprint, network, symbol, utc_ms()),
            )
            row = connection.execute(
                "SELECT id FROM scopes WHERE account_fingerprint=? AND network=? AND symbol=?",
                (account_fingerprint, network, symbol),
            ).fetchone()
            return int(row[0])

    def record_run(
        self, scope_id: int, run_id: str, *, strategy_type: str,
        config_version: str, allocation_equity: Any, started_at_ms: int | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO strategy_runs(scope_id,run_id,strategy_type,config_version,
                   allocation_equity,started_at_ms) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(scope_id,run_id) DO UPDATE SET
                   strategy_type=excluded.strategy_type, config_version=excluded.config_version,
                   allocation_equity=excluded.allocation_equity""",
                (scope_id, run_id, strategy_type, config_version,
                 decimal_string(allocation_equity), started_at_ms or utc_ms()),
            )

    def latest_allocated_run(
        self, scope_id: int, *, strategy_type: str, config_version: str
    ) -> dict[str, Any] | None:
        """Return the latest compatible run with a usable capital basis."""
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_runs
                   WHERE scope_id=? AND strategy_type=? AND config_version=?
                     AND CAST(allocation_equity AS NUMERIC) > 0
                   ORDER BY started_at_ms DESC LIMIT 1""",
                (scope_id, strategy_type, config_version),
            ).fetchone()
            return None if row is None else dict(row)

    def record_owned_order(
        self, scope_id: int, run_id: str, decision_id: str, ordinal: int, *,
        exchange_order_id: Any = None, client_order_id: Any = None,
    ) -> None:
        if exchange_order_id is None and client_order_id is None:
            return
        with self.transaction() as connection:
            predicates = []
            parameters: list[Any] = [scope_id]
            if exchange_order_id is not None:
                predicates.append("exchange_order_id=?")
                parameters.append(str(exchange_order_id))
            if client_order_id is not None:
                predicates.append("client_order_id=?")
                parameters.append(str(client_order_id))
            matches = connection.execute(
                f"SELECT scope_id,run_id,decision_id,ordinal FROM owned_orders "
                f"WHERE scope_id=? AND ({' OR '.join(predicates)})",
                parameters,
            ).fetchall()
            identities = {
                (row["scope_id"], row["run_id"], row["decision_id"], row["ordinal"])
                for row in matches
            }
            if len(identities) > 1:
                raise StrategyAnalyticsStoreError(
                    "analytics order identifiers map to different ownership records"
                )
            if identities:
                owner_scope, owner_run, owner_decision, owner_ordinal = identities.pop()
                connection.execute(
                    """UPDATE owned_orders SET
                       exchange_order_id=COALESCE(exchange_order_id,?),
                       client_order_id=COALESCE(client_order_id,?)
                       WHERE scope_id=? AND run_id=? AND decision_id=? AND ordinal=?""",
                    (
                        None if exchange_order_id is None else str(exchange_order_id),
                        None if client_order_id is None else str(client_order_id),
                        owner_scope,
                        owner_run,
                        owner_decision,
                        owner_ordinal,
                    ),
                )
                return
            connection.execute(
                """INSERT INTO owned_orders(scope_id,run_id,exchange_order_id,client_order_id,
                   decision_id,ordinal,captured_at_ms) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id,run_id,decision_id,ordinal) DO UPDATE SET
                   exchange_order_id=COALESCE(excluded.exchange_order_id,owned_orders.exchange_order_id),
                   client_order_id=COALESCE(excluded.client_order_id,owned_orders.client_order_id)""",
                (scope_id, run_id,
                 None if exchange_order_id is None else str(exchange_order_id),
                 None if client_order_id is None else str(client_order_id),
                 decision_id, ordinal, utc_ms()),
            )

    def import_execution_journal(self, scope_id: int, document: dict[str, Any]) -> int:
        run = document["run"]
        started = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
        metadata = run.get("metadata", {})
        allocation = metadata.get("capital_limit_usdt")
        if allocation is None:
            existing = next(
                (item for item in self.snapshot_rows(scope_id)["runs"]
                 if item["run_id"] == run["run_id"]),
                None,
            )
            allocation = existing["allocation_equity"] if existing else "0"
        self.record_run(
            scope_id, run["run_id"],
            strategy_type=metadata.get("strategy_type", "unknown"),
            config_version=metadata.get("config_version", "unknown"),
            allocation_equity=allocation,
            started_at_ms=int(started.timestamp() * 1000),
        )
        imported = 0
        for decision_id, decision in document.get("decisions", {}).items():
            for order in decision.get("orders", {}).values():
                result = order.get("result", {})
                if result.get("exchange_order_id") is None and not result.get("client_order_id"):
                    continue
                self.record_owned_order(
                    scope_id, run["run_id"], decision_id, int(order["ordinal"]),
                    exchange_order_id=result.get("exchange_order_id"),
                    client_order_id=result.get("client_order_id"),
                )
                imported += 1
        return imported

    def upsert_fills(self, scope_id: int, fills: list[dict[str, Any]]) -> int:
        rows = []
        for fill in fills:
            rows.append((scope_id, str(fill["id"]), str(fill["orderId"]),
                         fill.get("clientOrderId"), int(fill["time"]), str(fill["side"]).upper(),
                         decimal_string(fill["qty"]), decimal_string(fill["price"]),
                         decimal_string(fill.get("realizedPnl", "0")),
                         decimal_string(fill.get("commission", "0")), fill.get("commissionAsset")))
        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """INSERT INTO exchange_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id,exchange_trade_id) DO UPDATE SET
                   exchange_order_id=excluded.exchange_order_id,
                   client_order_id=excluded.client_order_id,time_ms=excluded.time_ms,
                   side=excluded.side,quantity=excluded.quantity,price=excluded.price,
                   realized_pnl=excluded.realized_pnl,commission=excluded.commission,
                   commission_asset=excluded.commission_asset""", rows,
            )
            return connection.total_changes - before

    def upsert_income(self, scope_id: int, events: list[dict[str, Any]]) -> int:
        rows = [(scope_id, str(item.get("tranId", item.get("id"))), int(item["time"]),
                 str(item.get("incomeType", "UNKNOWN")), item.get("asset"),
                 decimal_string(item.get("income", "0"))) for item in events]
        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """INSERT INTO income_events VALUES(?,?,?,?,?,?)
                   ON CONFLICT(scope_id,transaction_id,income_type) DO UPDATE SET
                   time_ms=excluded.time_ms,asset=excluded.asset,amount=excluded.amount""", rows,
            )
            return connection.total_changes - before

    def record_equity(self, scope_id: int, time_ms: int, equity: Any, *, capital_flow: Any = "0", mark_price: Any = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO equity_inputs(scope_id,time_ms,equity,capital_flow,mark_price)
                   VALUES(?,?,?,?,?) ON CONFLICT(scope_id,time_ms) DO UPDATE SET
                   equity=excluded.equity,capital_flow=excluded.capital_flow,mark_price=excluded.mark_price""",
                (scope_id, int(time_ms), decimal_string(equity), decimal_string(capital_flow),
                 None if mark_price is None else decimal_string(mark_price)),
            )

    def set_sync_state(self, scope_id: int, stream: str, *, cursor: Any = None,
                       coverage_start_ms: int | None = None, coverage_end_ms: int | None = None,
                       complete: bool = False, status: str = "partial", reason: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO sync_state VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id,stream) DO UPDATE SET cursor=excluded.cursor,
                   coverage_start_ms=excluded.coverage_start_ms,coverage_end_ms=excluded.coverage_end_ms,
                   complete=excluded.complete,status=excluded.status,reason=excluded.reason,
                   updated_at_ms=excluded.updated_at_ms""",
                (scope_id, stream, None if cursor is None else str(cursor), coverage_start_ms,
                 coverage_end_ms, int(complete), status, reason, utc_ms()),
            )

    def get_sync_state(self, scope_id: int, stream: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sync_state WHERE scope_id=? AND stream=?", (scope_id, stream)
            ).fetchone()
            return None if row is None else dict(row)

    def mark_integrity_flag(self, scope_id: int, flag: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO scope_integrity(scope_id,flag,detected_at_ms) VALUES(?,?,?)",
                (scope_id, flag, utc_ms()),
            )

    def has_integrity_flag(self, scope_id: int, flag: str) -> bool:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM scope_integrity WHERE scope_id=? AND flag=?",
                (scope_id, flag),
            ).fetchone()
            return row is not None

    def snapshot_rows(self, scope_id: int) -> dict[str, list[dict[str, Any]]]:
        self.initialize()
        with self._connection() as connection:
            queries = {
                "runs": "SELECT * FROM strategy_runs WHERE scope_id=? ORDER BY started_at_ms",
                "owned_orders": "SELECT * FROM owned_orders WHERE scope_id=? ORDER BY captured_at_ms",
                "fills": """SELECT f.*,
                    COALESCE(
                      (SELECT o.run_id FROM owned_orders o
                       WHERE o.scope_id=f.scope_id AND o.exchange_order_id=f.exchange_order_id LIMIT 1),
                      (SELECT o.run_id FROM owned_orders o
                       WHERE o.scope_id=f.scope_id AND o.client_order_id=f.client_order_id LIMIT 1)
                    ) AS owner_run_id
                    FROM exchange_fills f WHERE f.scope_id=? AND
                    (EXISTS(SELECT 1 FROM owned_orders o WHERE o.scope_id=f.scope_id AND o.exchange_order_id=f.exchange_order_id)
                     OR EXISTS(SELECT 1 FROM owned_orders o WHERE o.scope_id=f.scope_id AND o.client_order_id=f.client_order_id))
                    ORDER BY time_ms,exchange_trade_id""",
                "income": "SELECT * FROM income_events WHERE scope_id=? ORDER BY time_ms,transaction_id",
                "equity": "SELECT * FROM equity_inputs WHERE scope_id=? ORDER BY time_ms",
                "coverage": "SELECT * FROM sync_state WHERE scope_id=? ORDER BY stream",
            }
            return {name: [dict(row) for row in connection.execute(sql, (scope_id,))]
                    for name, sql in queries.items()}

    def owned_order_ids(self, scope_id: int) -> tuple[set[str], set[str]]:
        self.initialize()
        with self._connection() as connection:
            exchange = {row[0] for row in connection.execute(
                "SELECT exchange_order_id FROM owned_orders WHERE scope_id=? AND exchange_order_id IS NOT NULL",
                (scope_id,),
            )}
            client = {row[0] for row in connection.execute(
                "SELECT client_order_id FROM owned_orders WHERE scope_id=? AND client_order_id IS NOT NULL",
                (scope_id,),
            )}
            return exchange, client

    def scope_details(self, scope_id: int) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone()
            if row is None:
                raise StrategyAnalyticsStoreError("analytics scope does not exist")
            return dict(row)
