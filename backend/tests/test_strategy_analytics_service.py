from __future__ import annotations

from decimal import Decimal

from backend.app.services import strategy_analytics
from backend.app.services.binance_retry import BinanceRetryExecutor, BinanceRetryPolicy
from backend.app.services.binance_usdm_gateway import BinanceUsdMGateway
from backend.app.services.strategy_analytics import (
    HISTORY_LOOKBACK_MS,
    StrategyAnalyticsService,
    _derive_trades,
    _period_metrics,
)
from backend.app.services.strategy_analytics_store import StrategyAnalyticsStore
from backend.app.services.strategy_analytics_store import utc_ms


def _fill(trade_id, order_id, time, side, qty, pnl, commission="0"):
    return {"scope_id": 1, "exchange_trade_id": str(trade_id),
            "exchange_order_id": str(order_id), "client_order_id": None,
            "time_ms": time, "side": side, "quantity": qty, "price": "100",
            "realized_pnl": pnl, "commission": commission, "commission_asset": "USDT"}


def test_flat_cycles_include_adds_and_reversal_closes_then_opens():
    fills = [
        _fill(1, 1, 10, "BUY", "1", "0", "0.1"),
        _fill(2, 2, 20, "BUY", "1", "0", "0.1"),
        _fill(3, 3, 30, "SELL", "3", "20", "0.3"),
        _fill(4, 4, 40, "BUY", "1", "-5", "0.1"),
    ]
    income = [{"time_ms": 25, "income_type": "FUNDING_FEE", "amount": "-1"}]

    trades, commission, funding, open_trade, supported = _derive_trades(fills, income)

    assert len(trades) == 2
    assert trades[0]["net"] == Decimal("18.6")
    assert trades[1]["net"] == Decimal("-5.2")
    assert commission == Decimal("0.6")
    assert funding == Decimal("-1")
    assert open_trade is False
    assert supported is True


def test_snapshot_nulls_metrics_until_fill_and_income_coverage_are_complete(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="1000", started_at_ms=1)

    incomplete = service.snapshot(scope, as_of_ms=1_800_000_000_000)
    assert incomplete["week"]["net_pnl_usdt"] is None
    assert set(incomplete["coverage"]["reasons"]) == {"fills_not_synced", "income_not_synced"}

    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             coverage_end_ms=1_800_000_000_000,
                             complete=True, status="complete")
    complete = service.snapshot(scope, as_of_ms=1_800_000_000_000)
    assert complete["week"]["net_pnl_usdt"] == "0"
    assert complete["week"]["payoff_ratio"] is None
    assert complete["week"]["net_return_pct"] == "0"


def test_partial_coverage_keeps_attributable_completed_trade_metrics(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.record_owned_order(scope, "run", "open", 0, exchange_order_id=1)
    store.record_owned_order(scope, "run", "close", 0, exchange_order_id=2)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 10, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0.1",
         "commissionAsset": "USDT"},
        {"id": 2, "orderId": 2, "time": 20, "side": "SELL", "qty": "1",
         "price": "110", "realizedPnl": "10", "commission": "0.1",
         "commissionAsset": "USDT"},
    ])
    store.set_sync_state(scope, "fills", coverage_start_ms=1, complete=False,
                         status="partial", reason="history_retention_limit")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert snapshot["week"]["status"] == "partial"
    assert snapshot["week"]["net_pnl_usdt"] == "9.8"
    assert snapshot["week"]["net_return_pct"] == "9.800"
    assert snapshot["counts"] == {
        "status": "partial", "completed_total": 1, "long": 1, "short": 0,
    }
    assert snapshot["overall"] == {
        "status": "partial",
        "reasons": ["history_retention_limit", "income_not_synced"],
        "completed_count": 1,
        "long": 1,
        "short": 0,
        "win_count": 1,
        "loss_count": 0,
        "win_rate_pct": "100",
        "payoff_ratio": None,
    }


def test_unsupported_commission_only_hides_net_based_metrics(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.record_owned_order(scope, "run", "open", 0, exchange_order_id=1)
    store.record_owned_order(scope, "run", "close", 0, exchange_order_id=2)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 10, "side": "SELL", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0.001",
         "commissionAsset": "BNB"},
        {"id": 2, "orderId": 2, "time": 20, "side": "BUY", "qty": "1",
         "price": "90", "realizedPnl": "10", "commission": "0.001",
         "commissionAsset": "BNB"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert snapshot["counts"]["short"] == 1
    assert snapshot["counts"]["completed_total"] == 1
    assert snapshot["week"]["status"] == "unavailable"
    assert snapshot["week"]["net_pnl_usdt"] is None
    assert snapshot["overall"]["win_count"] is None
    assert snapshot["overall"]["loss_count"] is None
    assert snapshot["overall"]["win_rate_pct"] is None


def test_sync_uses_exact_ids_and_bounded_pagination(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, max_pages=1, page_size=2, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=utc_ms() - 1_000)
    store.record_owned_order(scope, "run", "d", 0, exchange_order_id=1)

    class Client:
        def futures_account_trades(self, **kwargs):
            return [
                {"id": 1, "orderId": 1, "time": 10, "side": "BUY", "qty": "1",
                 "price": "10", "realizedPnl": "0", "commission": "0", "commissionAsset": "USDT"},
                {"id": 2, "orderId": 2, "time": 11, "side": "BUY", "qty": "1",
                 "price": "10", "realizedPnl": "0", "commission": "0", "commissionAsset": "USDT"},
            ]
        def futures_income_history(self, **kwargs):
            return []

    result = service._sync_locked(Client(), scope, "SOLUSDT")

    assert result["status"] == "partial"
    assert len(store.snapshot_rows(scope)["fills"]) == 1
    assert store.get_sync_state(scope, "fills")["reason"] == "external_fills_present"


def test_fill_page_retry_preserves_cutoff_and_advances_cursor_once(monkeypatch, tmp_path):
    cutoff = 1_800_000_000_000
    started = cutoff - 1_000
    monkeypatch.setattr(strategy_analytics, "utc_ms", lambda: cutoff)
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(
        store, max_pages=2, page_size=2, cooldown_seconds=0
    )
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(
        scope,
        "run",
        strategy_type="sar",
        config_version="v3",
        allocation_equity="100",
        started_at_ms=started,
    )
    for order_id in (1, 2):
        store.record_owned_order(scope, "run", "d", order_id, exchange_order_id=order_id)

    class Client:
        def __init__(self):
            self.fill_calls = []
            self.income_calls = []

        def futures_account_trades(self, **kwargs):
            self.fill_calls.append(dict(kwargs))
            if len(self.fill_calls) == 1:
                raise ConnectionError("private transport details")
            if "fromId" in kwargs:
                return []
            return [
                {"id": order_id, "orderId": order_id, "time": started + order_id,
                 "side": "BUY", "qty": "1", "price": "10", "realizedPnl": "0",
                 "commission": "0", "commissionAsset": "USDT"}
                for order_id in (1, 2)
            ]

        def futures_income_history(self, **kwargs):
            self.income_calls.append(dict(kwargs))
            if len(self.income_calls) == 1:
                raise ConnectionError("private income transport details")
            return []

    client = Client()
    retry = BinanceRetryExecutor(
        policy=BinanceRetryPolicy(max_attempts=2, budget_seconds=1),
        sleeper=lambda _delay: None,
        rng=lambda: 0,
    )

    result = service._sync_locked(
        BinanceUsdMGateway(client, retry_executor=retry), scope, "SOLUSDT"
    )

    assert result["status"] == "complete"
    assert client.fill_calls[0] == client.fill_calls[1]
    assert client.fill_calls[2]["fromId"] == 3
    assert {call["endTime"] for call in client.fill_calls} == {cutoff}
    assert len(client.income_calls) == 2
    assert client.income_calls[0] == client.income_calls[1]
    assert client.income_calls[0]["endTime"] == cutoff
    assert store.get_sync_state(scope, "fills")["cursor"] == "2"


def test_sync_starts_from_owned_run_without_allocation(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - 10_000
    store.record_run(scope, "legacy", strategy_type="sar", config_version="v2",
                     allocation_equity="0", started_at_ms=started)
    store.record_owned_order(scope, "legacy", "open", 0, exchange_order_id=1)

    class Client:
        def futures_account_trades(self, **kwargs):
            assert kwargs["startTime"] == started
            return []

        def futures_income_history(self, **kwargs):
            assert kwargs["startTime"] == started
            return []

    service._sync_locked(Client(), scope, "SOLUSDT")


def test_external_fill_reason_persists_after_later_clean_sync(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - 1_000
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=started)
    store.record_owned_order(scope, "run", "open", 0, exchange_order_id=1)

    class ExternalClient:
        def futures_account_trades(self, **_kwargs):
            return [{"id": 2, "orderId": 2, "time": started + 1, "side": "BUY",
                     "qty": "1", "price": "10", "realizedPnl": "0",
                     "commission": "0", "commissionAsset": "USDT"}]

        def futures_income_history(self, **_kwargs):
            return []

    class CleanClient:
        def futures_account_trades(self, **_kwargs):
            return []

        def futures_income_history(self, **_kwargs):
            return []

    service._sync_locked(ExternalClient(), scope, "SOLUSDT")
    result = service._sync_locked(CleanClient(), scope, "SOLUSDT")

    assert result["status"] == "partial"
    assert "external_fills_present" in result["reasons"]
    assert store.get_sync_state(scope, "fills")["reason"] == "external_fills_present"


def test_external_fill_flag_survives_retention_reason(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - HISTORY_LOOKBACK_MS - 1_000
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=started)
    store.record_owned_order(scope, "run", "open", 0, exchange_order_id=1)

    class Client:
        def futures_account_trades(self, **_kwargs):
            return [{"id": 2, "orderId": 2, "time": utc_ms(), "side": "BUY",
                     "qty": "1", "price": "10", "realizedPnl": "0",
                     "commission": "0", "commissionAsset": "USDT"}]

        def futures_income_history(self, **_kwargs):
            return []

    result = service._sync_locked(Client(), scope, "SOLUSDT")
    snapshot = service.snapshot(scope)

    assert "history_retention_limit" in result["reasons"]
    assert "external_fills_present" in result["reasons"]
    assert "external_fills_present" in snapshot["coverage"]["reasons"]


def test_equity_curve_removes_capital_flows_from_profit(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.record_equity(scope, 10, "100")
    store.record_equity(scope, 20, "160", capital_flow="50", mark_price="12")
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, complete=True, status="complete")

    curve = service.snapshot(scope)["equity_curve"]

    assert curve[-1]["equity_usdt"] == "160"
    assert curve[-1]["net_pnl_usdt"] == "10"
    assert curve[-1]["mark_price"] == "12"


def test_snapshot_exposes_public_contract_without_account_fingerprint(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:private-account", "testnet", "SOLUSDT")
    store.record_run(
        scope,
        "run",
        strategy_type="sar",
        config_version="v3",
        allocation_equity="1000",
        started_at_ms=1,
    )
    for stream in ("fills", "income"):
        store.set_sync_state(
            scope,
            stream,
            coverage_start_ms=1,
            coverage_end_ms=1_800_000_000_000,
            complete=True,
            status="complete",
        )

    snapshot = service.snapshot(scope, as_of_ms=1_800_000_000_000)

    assert "account_fingerprint" not in snapshot["scope"]
    assert snapshot["counts"] == {
        "status": "complete",
        "completed_total": 0,
        "long": 0,
        "short": 0,
    }
    assert snapshot["equity_curve"][0]["equity_usdt"] == "1000"


def test_period_return_chain_links_different_allocations():
    trades = [
        {"closed_at_ms": 20, "net": Decimal("10"), "allocation": Decimal("100"),
         "run_id": "run-1"},
        {"closed_at_ms": 30, "net": Decimal("20"), "allocation": Decimal("200"),
         "run_id": "run-2"},
    ]

    metrics = _period_metrics(trades, 1, True, [])

    assert metrics["net_pnl_usdt"] == "30"
    assert metrics["net_return_pct"] == "21.00"


def test_period_return_aggregates_trades_within_one_allocation_run():
    trades = [
        {"closed_at_ms": 20, "net": Decimal("10"), "allocation": Decimal("100"),
         "run_id": "run-1"},
        {"closed_at_ms": 30, "net": Decimal("10"), "allocation": Decimal("100"),
         "run_id": "run-1"},
    ]

    metrics = _period_metrics(trades, 1, True, [])

    assert metrics["net_pnl_usdt"] == "20"
    assert metrics["net_return_pct"] == "20.0"


def test_resume_keeps_flat_to_flat_trade_in_one_analytics_run(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope, run_id = service.capture_run(
        "sha256:account", "testnet", "SOLUSDT", run_id="session-1",
        strategy_type="sar", config_version="v3", allocation_equity="100",
    )
    service.capture_order(scope, run_id, "open", 0,
                          exchange_order_id=1, client_order_id=None)

    resumed_scope, resumed_run = service.capture_run(
        "sha256:account", "testnet", "SOLUSDT", run_id="session-2",
        strategy_type="sar", config_version="v3", allocation_equity="100",
        resume_existing=True,
    )
    service.capture_order(resumed_scope, resumed_run, "close", 0,
                          exchange_order_id=2, client_order_id=None)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 10, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 2, "orderId": 2, "time": 20, "side": "SELL", "qty": "1",
         "price": "110", "realizedPnl": "10", "commission": "0",
         "commissionAsset": "USDT"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             coverage_end_ms=1_800_000_000_000,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert resumed_scope == scope
    assert resumed_run == run_id
    assert snapshot["counts"]["completed_total"] == 1
    assert snapshot["week"]["net_pnl_usdt"] == "10"


def test_resume_rejects_allocation_change_while_position_is_open(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    service.capture_run(
        "sha256:account", "testnet", "SOLUSDT", run_id="session-1",
        strategy_type="sar", config_version="v3", allocation_equity="100",
    )

    try:
        service.capture_run(
            "sha256:account", "testnet", "SOLUSDT", run_id="session-2",
            strategy_type="sar", config_version="v3", allocation_equity="200",
            resume_existing=True,
        )
    except ValueError as exc:
        assert "capital allocation cannot change" in str(exc)
    else:
        raise AssertionError("allocation change must be rejected during position recovery")


def test_owned_legacy_run_without_allocation_marks_snapshot_incomplete(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "allocated", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.record_run(scope, "legacy", strategy_type="sar", config_version="v3",
                     allocation_equity="0", started_at_ms=2)
    store.record_owned_order(scope, "legacy", "open", 0, exchange_order_id=7)
    store.record_owned_order(scope, "legacy", "close", 0, exchange_order_id=8)
    store.upsert_fills(scope, [
        {"id": 7, "orderId": 7, "time": 10, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0.1",
         "commissionAsset": "USDT"},
        {"id": 8, "orderId": 8, "time": 20, "side": "SELL", "qty": "1",
         "price": "110", "realizedPnl": "10", "commission": "0.1",
         "commissionAsset": "USDT"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert "allocation_basis_missing" in snapshot["coverage"]["reasons"]
    assert snapshot["counts"] == {
        "status": "partial", "completed_total": 1, "long": 1, "short": 0,
    }
    assert snapshot["overall"]["win_rate_pct"] == "100"
    assert snapshot["week"]["net_pnl_usdt"] == "9.8"
    assert snapshot["week"]["net_return_pct"] is None
    assert snapshot["week"]["return_status"] == "unavailable"


def test_complete_local_coverage_survives_exchange_retention_window(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - (90 * 24 * 60 * 60 * 1000)
    future = utc_ms() + 10_000
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=started)
    store.set_sync_state(scope, "fills", cursor=42, coverage_start_ms=started,
                         coverage_end_ms=future, complete=True, status="complete")
    store.set_sync_state(scope, "income", cursor=future, coverage_start_ms=started,
                         coverage_end_ms=future, complete=True, status="complete")

    class Client:
        def futures_account_trades(self, **kwargs):
            assert kwargs["fromId"] == 43
            return []

        def futures_income_history(self, **_kwargs):
            raise AssertionError("future cursor should not request historical income")

    result = service._sync_locked(Client(), scope, "SOLUSDT")

    assert result["status"] == "complete"
    assert store.get_sync_state(scope, "fills")["coverage_start_ms"] == started
    assert store.get_sync_state(scope, "income")["coverage_start_ms"] == started


def test_sync_failure_preserves_complete_history_cursor_and_start(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - (90 * 24 * 60 * 60 * 1000)
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=started)
    for stream, cursor in (("fills", 42), ("income", started + 1_000)):
        store.set_sync_state(scope, stream, cursor=cursor,
                             coverage_start_ms=started, coverage_end_ms=utc_ms(),
                             complete=True, status="complete")

    class Client:
        def futures_account_trades(self, **_kwargs):
            raise ConnectionError("temporary fill failure")

        def futures_income_history(self, **_kwargs):
            raise ConnectionError("temporary income failure")

    result = service._sync_locked(Client(), scope, "SOLUSDT")

    assert result["status"] == "partial"
    fill_state = store.get_sync_state(scope, "fills")
    income_state = store.get_sync_state(scope, "income")
    assert fill_state["cursor"] == "42"
    assert income_state["cursor"] == str(started + 1_000)
    assert fill_state["coverage_start_ms"] == started
    assert income_state["coverage_start_ms"] == started

    class RecoveredClient:
        def futures_account_trades(self, **kwargs):
            assert kwargs["fromId"] == 43
            return []

        def futures_income_history(self, **_kwargs):
            return []

    recovered = service._sync_locked(RecoveredClient(), scope, "SOLUSDT")

    assert recovered["status"] == "complete"
    assert store.get_sync_state(scope, "fills")["complete"] == 1
    assert store.get_sync_state(scope, "income")["complete"] == 1


def test_open_cycle_makes_costs_and_counts_insufficient(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(
        scope,
        "run",
        strategy_type="sar",
        config_version="v3",
        allocation_equity="1000",
        started_at_ms=1,
    )
    store.record_owned_order(scope, "run", "d", 0, exchange_order_id=1)
    store.upsert_fills(scope, [{
        "id": 1,
        "orderId": 1,
        "time": 10,
        "side": "BUY",
        "qty": "1",
        "price": "100",
        "realizedPnl": "0",
        "commission": "0.1",
        "commissionAsset": "USDT",
    }])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_800_000_000_000)

    assert "open_position_costs_incomplete" in snapshot["coverage"]["reasons"]
    assert snapshot["costs"]["commission_usdt"] is None
    assert snapshot["counts"]["status"] == "partial"
    assert snapshot["counts"]["completed_total"] == 0


def test_snapshot_excludes_run_when_initial_position_baseline_is_unknown(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.record_owned_order(scope, "run", "open", 0, exchange_order_id=1)
    store.record_owned_order(scope, "run", "close", 0, exchange_order_id=2)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 20, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 2, "orderId": 2, "time": 30, "side": "SELL", "qty": "1",
         "price": "110", "realizedPnl": "10", "commission": "0",
         "commissionAsset": "USDT"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=10,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert "position_baseline_unknown" in snapshot["coverage"]["reasons"]
    assert snapshot["counts"]["completed_total"] is None
    assert snapshot["week"]["net_pnl_usdt"] is None


def test_snapshot_hides_path_metrics_for_external_fills(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    store.record_run(scope, "run", strategy_type="sar", config_version="v3",
                     allocation_equity="100", started_at_ms=1)
    store.set_sync_state(scope, "fills", coverage_start_ms=1, complete=False,
                         status="partial", reason="external_fills_present")
    store.mark_integrity_flag(scope, "external_fills_present")
    store.set_sync_state(scope, "income", coverage_start_ms=1, complete=True,
                         status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert snapshot["counts"]["completed_total"] is None
    assert snapshot["counts"]["long"] is None
    assert snapshot["overall"]["win_rate_pct"] is None
    assert snapshot["week"]["net_pnl_usdt"] is None


def test_snapshot_hides_path_metrics_for_overlapping_runs(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    for run_id in ("run-1", "run-2"):
        store.record_run(scope, run_id, strategy_type="sar", config_version="v3",
                         allocation_equity="100", started_at_ms=1)
    for run_id, open_id, close_id in (("run-1", 1, 2), ("run-2", 3, 4)):
        store.record_owned_order(scope, run_id, "open", 0, exchange_order_id=open_id)
        store.record_owned_order(scope, run_id, "close", 0, exchange_order_id=close_id)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 10, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 3, "orderId": 3, "time": 20, "side": "SELL", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 2, "orderId": 2, "time": 30, "side": "SELL", "qty": "1",
         "price": "110", "realizedPnl": "10", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 4, "orderId": 4, "time": 40, "side": "BUY", "qty": "1",
         "price": "90", "realizedPnl": "10", "commission": "0",
         "commissionAsset": "USDT"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert "strategy_run_overlap" in snapshot["coverage"]["reasons"]
    assert snapshot["counts"]["completed_total"] is None
    assert snapshot["week"]["net_pnl_usdt"] is None


def test_open_run_overlaps_later_closed_run_until_snapshot_time(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    for run_id in ("open-run", "closed-run"):
        store.record_run(scope, run_id, strategy_type="sar", config_version="v3",
                         allocation_equity="100", started_at_ms=1)
    store.record_owned_order(scope, "open-run", "open", 0, exchange_order_id=1)
    store.record_owned_order(scope, "closed-run", "open", 0, exchange_order_id=2)
    store.record_owned_order(scope, "closed-run", "close", 0, exchange_order_id=3)
    store.upsert_fills(scope, [
        {"id": 1, "orderId": 1, "time": 10, "side": "BUY", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 2, "orderId": 2, "time": 20, "side": "SELL", "qty": "1",
         "price": "100", "realizedPnl": "0", "commission": "0",
         "commissionAsset": "USDT"},
        {"id": 3, "orderId": 3, "time": 30, "side": "BUY", "qty": "1",
         "price": "90", "realizedPnl": "10", "commission": "0",
         "commissionAsset": "USDT"},
    ])
    for stream in ("fills", "income"):
        store.set_sync_state(scope, stream, coverage_start_ms=1,
                             complete=True, status="complete")

    snapshot = service.snapshot(scope, as_of_ms=1_000)

    assert "strategy_run_overlap" in snapshot["coverage"]["reasons"]
    assert snapshot["overall"]["win_rate_pct"] is None
    assert snapshot["week"]["net_pnl_usdt"] is None


def test_empty_incremental_fill_page_preserves_cursor(tmp_path):
    store = StrategyAnalyticsStore(tmp_path / "analytics.sqlite3")
    service = StrategyAnalyticsService(store, cooldown_seconds=0)
    scope = store.ensure_scope("sha256:account", "testnet", "SOLUSDT")
    started = utc_ms() - 1_000
    store.record_run(
        scope,
        "run",
        strategy_type="sar",
        config_version="v3",
        allocation_equity="100",
        started_at_ms=started,
    )
    store.set_sync_state(
        scope,
        "fills",
        cursor=42,
        coverage_start_ms=started,
        coverage_end_ms=started,
        complete=True,
        status="complete",
    )

    class Client:
        def futures_account_trades(self, **kwargs):
            assert kwargs["fromId"] == 43
            return []

        def futures_income_history(self, **kwargs):
            return []

    service._sync_locked(Client(), scope, "SOLUSDT")

    assert store.get_sync_state(scope, "fills")["cursor"] == "42"
