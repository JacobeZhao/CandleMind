from __future__ import annotations

from datetime import datetime, timezone

from backend.app.services.account_trade_analytics import (
    AccountTradeAnalyticsService,
    SEVEN_DAYS_MS,
    SIX_MONTHS_MS,
    derive_flat_to_flat,
)
from backend.app.services.binance_usdm_gateway import ExchangeScope
from backend.app.services.binance_usdm_gateway import BinanceGatewayAuthenticationError


def _fill(
    trade_id: int,
    time: int,
    side: str,
    qty: str,
    pnl: str = "0",
    commission: str = "0",
) -> dict:
    return {
        "id": trade_id,
        "time": time,
        "side": side,
        "qty": qty,
        "realizedPnl": pnl,
        "commission": commission,
    }


def test_flat_to_flat_supports_layers_partial_close_and_reversal() -> None:
    cycles, baseline_unknown = derive_flat_to_flat([
        _fill(1, 1, "BUY", "1", commission="1"),
        _fill(2, 2, "BUY", "1", commission="1"),
        _fill(3, 3, "SELL", "1", pnl="10", commission="1"),
        _fill(4, 4, "SELL", "2", pnl="20", commission="2"),
        _fill(5, 5, "BUY", "1", pnl="-5", commission="1"),
    ])

    assert baseline_unknown is False
    assert [(row["direction"], row["net"]) for row in cycles] == [
        ("LONG", 26),
        ("SHORT", -7),
    ]


def test_first_closing_fill_is_excluded_when_position_baseline_is_unknown() -> None:
    cycles, baseline_unknown = derive_flat_to_flat([
        _fill(1, 1, "SELL", "1", pnl="5"),
        _fill(2, 2, "BUY", "1", pnl="0"),
        _fill(3, 3, "SELL", "1", pnl="2"),
        _fill(4, 4, "BUY", "1", pnl="3"),
    ])

    assert baseline_unknown is True
    assert len(cycles) == 1
    assert cycles[0]["direction"] == "SHORT"


def test_hedge_position_sides_are_derived_independently() -> None:
    rows = [
        {**_fill(1, 1, "BUY", "1"), "positionSide": "LONG"},
        {**_fill(2, 2, "SELL", "2"), "positionSide": "SHORT"},
        {**_fill(3, 3, "SELL", "1", pnl="3"), "positionSide": "LONG"},
        {**_fill(4, 4, "BUY", "2", pnl="4"), "positionSide": "SHORT"},
    ]

    cycles, baseline_unknown = derive_flat_to_flat(rows)

    assert baseline_unknown is False
    assert [(row["direction"], row["net"]) for row in cycles] == [
        ("LONG", 3), ("SHORT", 4)
    ]


class WindowGateway:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def account_trades(self, **params):
        self.calls.append(params)
        if "fromId" in params:
            return [row for row in self.rows if row["id"] >= params["fromId"]]
        return [
            row for row in self.rows
            if params["startTime"] <= row["time"] <= params["endTime"]
        ]


def test_snapshot_returns_eight_metrics_and_explicitly_unavailable_returns() -> None:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    gateway = WindowGateway([
        _fill(1, now - 3_000, "BUY", "1", commission="1"),
        _fill(2, now - 2_000, "SELL", "1", pnl="11", commission="1"),
        _fill(3, now - 1_000, "SELL", "1", commission="1"),
        _fill(4, now, "BUY", "1", pnl="-4", commission="1"),
    ])
    scope = ExchangeScope("sha256:test", "mainnet", "SOLUSDT")

    payload = AccountTradeAnalyticsService(cache_seconds=60).snapshot(gateway, scope)

    assert payload["scope"] == {
        "network": "mainnet", "symbol": "SOLUSDT", "basis": "account"
    }
    assert payload["week"]["net_pnl_usdt"] == "3"
    assert payload["week"]["net_return_pct"] is None
    assert payload["week"]["return_status"] == "unavailable"
    assert payload["month"]["net_pnl_usdt"] == "3"
    assert payload["counts"]["long"] == 1
    assert payload["counts"]["short"] == 1
    assert payload["overall"]["win_rate_pct"] == "50"
    assert payload["overall"]["payoff_ratio"] == "1.5"
    assert payload["coverage"]["status"] == "complete"
    assert len(gateway.calls) == (SIX_MONTHS_MS + SEVEN_DAYS_MS - 1) // SEVEN_DAYS_MS

class DenseGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def account_trades(self, **params):
        self.calls.append(params)
        if params["endTime"] - params["startTime"] > 1:
            return [_fill(index, params["startTime"], "BUY", "1") for index in range(1000)]
        return []


def test_dense_windows_split_and_report_partial_at_request_limit() -> None:
    gateway = DenseGateway()
    scope = ExchangeScope("sha256:test", "testnet", "BTCUSDT")

    payload = AccountTradeAnalyticsService(max_requests=2).snapshot(gateway, scope)

    assert payload["coverage"]["status"] == "partial"
    assert "pagination_limit" in payload["coverage"]["reasons"]
    assert payload["coverage"]["request_count"] == 2
    assert gateway.calls[1]["endTime"] < gateway.calls[0]["endTime"]


def test_expired_cache_uses_trade_id_incremental_sync() -> None:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    gateway = WindowGateway([
        _fill(1, now - 1, "BUY", "1"),
        _fill(2, now, "SELL", "1", pnl="2"),
    ])
    scope = ExchangeScope("sha256:incremental", "mainnet", "SOLUSDT")
    service = AccountTradeAnalyticsService(cache_seconds=0)

    service.snapshot(gateway, scope)
    gateway.calls.clear()
    service.snapshot(gateway, scope)

    assert gateway.calls == [{"symbol": "SOLUSDT", "limit": 1000, "fromId": 3}]


def test_non_usdt_commission_makes_profit_metrics_unavailable() -> None:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    opening = {**_fill(1, now - 1, "BUY", "1", commission="0.1"),
               "commissionAsset": "BNB"}
    closing = {**_fill(2, now, "SELL", "1", pnl="10")}
    gateway = WindowGateway([opening, closing])
    scope = ExchangeScope("sha256:test", "mainnet", "SOLUSDT")

    payload = AccountTradeAnalyticsService().snapshot(gateway, scope)

    assert payload["week"]["status"] == "unavailable"
    assert payload["week"]["net_pnl_usdt"] is None
    assert payload["week"]["reasons"] == [
        "commission_asset_conversion_unavailable"
    ]
    assert payload["counts"]["completed_total"] == 1


def test_backfill_keeps_recent_rows_when_an_older_window_is_rejected() -> None:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    class InterruptedGateway(WindowGateway):
        def account_trades(self, **params):
            if self.calls:
                raise BinanceGatewayAuthenticationError("IP changed")
            return super().account_trades(**params)

    gateway = InterruptedGateway([
        _fill(1, now - 2_000, "BUY", "1"),
        _fill(2, now - 1_000, "SELL", "1", pnl="5"),
    ])

    payload = AccountTradeAnalyticsService().snapshot(
        gateway, ExchangeScope("sha256:test", "mainnet", "SOLUSDT")
    )

    assert payload["coverage"]["status"] == "partial"
    assert payload["counts"]["completed_total"] == 1
    assert payload["week"]["net_pnl_usdt"] == "5"
