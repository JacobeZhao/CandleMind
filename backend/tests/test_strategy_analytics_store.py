from __future__ import annotations

import sqlite3

from backend.app.services.strategy_analytics_store import StrategyAnalyticsStore


def test_store_initializes_versioned_wal_database_and_isolates_scopes(tmp_path):
    path = tmp_path / "analytics" / "strategy_analytics.sqlite3"
    store = StrategyAnalyticsStore(path)
    first = store.ensure_scope("sha256:first", "testnet", "SOLUSDT")
    second = store.ensure_scope("sha256:second", "testnet", "SOLUSDT")

    assert first != second
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "1"
    with store._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_initialize_backfills_legacy_external_fill_integrity_flag(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    store = StrategyAnalyticsStore(path)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.set_sync_state(scope, "fills", complete=False, status="partial",
                         reason="external_fills_present")
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM scope_integrity")
        connection.commit()

    store.initialize()

    assert store.has_integrity_flag(scope, "external_fills_present") is True


def test_exact_owned_orders_filter_fills_and_upserts_are_idempotent(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    scope = store.ensure_scope("sha256:account", "mainnet", "BTCUSDT")
    store.record_run(scope, "run-1", strategy_type="sar", config_version="v3",
                     allocation_equity="1000", started_at_ms=1)
    store.record_owned_order(scope, "run-1", "decision-1", 0,
                             exchange_order_id=42, client_order_id="exact-id")
    owned = {"id": 1, "orderId": 42, "clientOrderId": "exact-id", "time": 10,
             "side": "BUY", "qty": "1.00", "price": "100", "realizedPnl": "0",
             "commission": "0.10", "commissionAsset": "USDT"}
    unrelated = {**owned, "id": 2, "orderId": 99, "clientOrderId": "cm-prefix-only"}

    store.upsert_fills(scope, [owned, unrelated])
    store.upsert_fills(scope, [owned])

    rows = store.snapshot_rows(scope)["fills"]
    assert [row["exchange_trade_id"] for row in rows] == ["1"]
    assert rows[0]["quantity"] == "1.00"


def test_execution_journal_import_captures_only_authoritative_result_ids(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    document = {
        "run": {"run_id": "run-1", "started_at": "2026-08-20T00:00:00Z",
                "metadata": {"strategy_type": "sar", "config_version": "v3"}},
        "decisions": {"d1": {"orders": {"0": {"ordinal": 0, "result": {
            "exchange_order_id": 7, "client_order_id": "owned"}}}}},
    }

    assert store.import_execution_journal(scope, document) == 1
    assert store.owned_order_ids(scope) == ({"7"}, {"owned"})


def test_journal_import_does_not_erase_captured_strategy_allocation(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run-1", strategy_type="sar", config_version="v3",
                     allocation_equity="250", started_at_ms=1)
    document = {
        "run": {"run_id": "run-1", "started_at": "2026-08-20T00:00:00Z",
                "metadata": {"strategy_type": "sar", "config_version": "v3"}},
        "decisions": {},
    }

    store.import_execution_journal(scope, document)

    assert store.snapshot_rows(scope)["runs"][0]["allocation_equity"] == "250"


def test_journal_reimport_keeps_existing_live_order_owner(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(
        scope,
        "live-session",
        strategy_type="sar",
        config_version="v3",
        allocation_equity="250",
        started_at_ms=1,
    )
    store.record_owned_order(
        scope,
        "live-session",
        "decision-1",
        0,
        exchange_order_id=7,
        client_order_id="owned",
    )
    document = {
        "run": {
            "run_id": "execution-journal",
            "started_at": "2026-08-20T00:00:00Z",
            "metadata": {"strategy_type": "sar", "config_version": "v3"},
        },
        "decisions": {
            "decision-1": {
                "orders": {
                    "0": {
                        "ordinal": 0,
                        "result": {
                            "exchange_order_id": 7,
                            "client_order_id": "owned",
                        },
                    }
                }
            }
        },
    }

    assert store.import_execution_journal(scope, document) == 1
    with store._connection() as connection:
        owners = connection.execute(
            "SELECT run_id FROM owned_orders WHERE scope_id=?",
            (scope,),
        ).fetchall()
    assert [row[0] for row in owners] == ["live-session"]


def test_latest_allocated_run_ignores_legacy_and_incompatible_runs(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "valid", strategy_type="sar", config_version="v3",
                     allocation_equity="250", started_at_ms=10)
    store.record_run(scope, "legacy", strategy_type="sar", config_version="v3",
                     allocation_equity="0", started_at_ms=20)
    store.record_run(scope, "other", strategy_type="other", config_version="v3",
                     allocation_equity="300", started_at_ms=30)

    run = store.latest_allocated_run(
        scope, strategy_type="sar", config_version="v3"
    )

    assert run is not None
    assert run["run_id"] == "valid"
