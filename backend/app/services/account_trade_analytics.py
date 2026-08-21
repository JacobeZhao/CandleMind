"""Deterministic account-level trade analytics for one USD-M symbol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import Any

from .binance_usdm_gateway import BinanceGatewayError, BinanceUsdMGateway, ExchangeScope


# Stay inside Binance's rolling six-month retention boundary for every month mix.
SIX_MONTHS_MS = 179 * 24 * 60 * 60 * 1000
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("trade contains an invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("trade contains a non-finite decimal")
    return result


def _string(value: Decimal) -> str:
    return format(value, "f")


def _period_start(now: datetime, period: str) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    else:
        start = start.replace(day=1)
    return int(start.timestamp() * 1000)


@dataclass
class _CacheEntry:
    stored_at: float
    payload: dict[str, Any]
    trades: dict[str, dict[str, Any]]
    complete: bool
    synced_through_ms: int


def _derive_one_way(trades: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    ordered = sorted(trades, key=lambda row: (int(row.get("time", 0)), int(row.get("id", 0))))
    position = Decimal("0")
    active: dict[str, Any] | None = None
    cycles: list[dict[str, Any]] = []
    baseline_unknown = bool(ordered and _decimal(ordered[0].get("realizedPnl", "0")) != 0)
    discard_until_flat = baseline_unknown

    for fill in ordered:
        quantity = abs(_decimal(fill.get("qty", "0")))
        if quantity == 0:
            continue
        signed = quantity if fill.get("side") == "BUY" else -quantity
        commission = abs(_decimal(fill.get("commission", "0")))
        realized = _decimal(fill.get("realizedPnl", "0"))
        remaining = signed
        realized_assigned = False

        while remaining:
            if position == 0:
                active = {
                    "direction": "LONG" if remaining > 0 else "SHORT",
                    "opened_at_ms": int(fill.get("time", 0)),
                    "closed_at_ms": None,
                    "realized_pnl": Decimal("0"),
                    "commission": Decimal("0"),
                    "commission_supported": True,
                }
            same_direction = position == 0 or (position > 0) == (remaining > 0)
            piece = remaining if same_direction else (
                min(abs(position), abs(remaining)) * (Decimal("1") if remaining > 0 else Decimal("-1"))
            )
            ratio = abs(piece) / quantity
            assert active is not None
            active["commission"] += commission * ratio
            if commission and fill.get("commissionAsset") not in (None, "", "USDT"):
                active["commission_supported"] = False
            if not same_direction and not realized_assigned:
                active["realized_pnl"] += realized
                realized_assigned = True
            position += piece
            remaining -= piece
            if position == 0:
                active["closed_at_ms"] = int(fill.get("time", 0))
                active["net"] = active["realized_pnl"] - active["commission"]
                if not discard_until_flat:
                    cycles.append(active)
                discard_until_flat = False
                active = None
    return cycles, baseline_unknown


def derive_flat_to_flat(trades: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Create flat-to-flat cycles without mixing hedge-mode position sides."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        position_side = str(trade.get("positionSide") or "BOTH").upper()
        groups.setdefault(position_side, []).append(trade)
    cycles: list[dict[str, Any]] = []
    baseline_unknown = False
    for rows in groups.values():
        group_cycles, group_unknown = _derive_one_way(rows)
        cycles.extend(group_cycles)
        baseline_unknown = baseline_unknown or group_unknown
    cycles.sort(key=lambda row: (row["closed_at_ms"], row["opened_at_ms"]))
    return cycles, baseline_unknown


class AccountTradeAnalyticsService:
    def __init__(self, *, cache_seconds: float = 15.0, max_requests: int = 128) -> None:
        self.cache_seconds = cache_seconds
        self.max_requests = max_requests
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._lock = Lock()

    def snapshot(self, gateway: BinanceUsdMGateway, scope: ExchangeScope) -> dict[str, Any]:
        key = (scope.account_fingerprint, scope.network, scope.symbol)
        with self._lock:
            cached = self._cache.get(key)
            if cached and monotonic() - cached.stored_at < self.cache_seconds:
                return cached.payload
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if cached is None:
            rows, request_count, complete = self._fetch_retained(
                gateway, scope.symbol, now_ms
            )
            indexed = {str(row["id"]): row for row in rows}
        else:
            updates, request_count, incremental_complete = self._fetch_incremental(
                gateway, scope.symbol, cached, now_ms
            )
            indexed = {**cached.trades, **{str(row["id"]): row for row in updates}}
            complete = cached.complete and incremental_complete
        payload = self._build(scope, list(indexed.values()), now_ms, request_count, complete)
        with self._lock:
            self._cache[key] = _CacheEntry(
                monotonic(), payload, indexed, complete, now_ms
            )
        return payload

    def _fetch_incremental(
        self,
        gateway: BinanceUsdMGateway,
        symbol: str,
        cached: _CacheEntry,
        now_ms: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        rows: dict[str, dict[str, Any]] = {}
        requests = 0
        latest_id = max((int(value) for value in cached.trades), default=None)
        while requests < self.max_requests:
            params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
            if latest_id is None:
                params.update({
                    "startTime": cached.synced_through_ms,
                    "endTime": now_ms,
                })
            else:
                params["fromId"] = latest_id + 1
            page = gateway.account_trades(**params)
            requests += 1
            for trade in page:
                trade_id = trade.get("id")
                if trade_id is None:
                    return list(rows.values()), requests, False
                rows[str(trade_id)] = trade
            if len(page) < 1000:
                return list(rows.values()), requests, True
            next_id = max(int(trade["id"]) for trade in page)
            if latest_id is not None and next_id <= latest_id:
                return list(rows.values()), requests, False
            latest_id = next_id
        return list(rows.values()), requests, False

    def _fetch_retained(
        self, gateway: BinanceUsdMGateway, symbol: str, now_ms: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        start = now_ms - SIX_MONTHS_MS
        requests = 0
        complete = True
        rows: dict[str, dict[str, Any]] = {}
        windows = [(point, min(point + SEVEN_DAYS_MS - 1, now_ms))
                   for point in range(start, now_ms + 1, SEVEN_DAYS_MS)]
        # Pull recent windows first so current week/month remain useful if an
        # unstable proxy or IP allowlist interrupts the historical backfill.
        pending = list(windows)
        while pending:
            window_start, window_end = pending.pop()
            if requests >= self.max_requests:
                complete = False
                break
            try:
                page = gateway.account_trades(
                    symbol=symbol, startTime=window_start, endTime=window_end, limit=1000
                )
            except BinanceGatewayError:
                if requests == 0:
                    raise
                complete = False
                break
            requests += 1
            if len(page) >= 1000:
                if window_start >= window_end:
                    complete = False
                    continue
                midpoint = (window_start + window_end) // 2
                pending.extend([(midpoint + 1, window_end), (window_start, midpoint)])
                continue
            for trade in page:
                trade_id = trade.get("id")
                if trade_id is None:
                    complete = False
                    continue
                rows[str(trade_id)] = trade
        return list(rows.values()), requests, complete

    @staticmethod
    def _build(
        scope: ExchangeScope,
        rows: list[dict[str, Any]],
        now_ms: int,
        request_count: int,
        complete: bool,
    ) -> dict[str, Any]:
        cycles, baseline_unknown = derive_flat_to_flat(rows)
        coverage_status = "complete" if complete and not baseline_unknown else "partial"
        reasons = []
        if not complete:
            reasons.append("pagination_limit")
        if baseline_unknown:
            reasons.append("initial_position_baseline_unknown")
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

        def metrics(start_ms: int) -> dict[str, Any]:
            selected = [cycle for cycle in cycles if cycle["closed_at_ms"] >= start_ms]
            commission_supported = all(
                cycle["commission_supported"] for cycle in selected
            )
            wins = [cycle for cycle in selected if cycle["net"] > 0]
            losses = [cycle for cycle in selected if cycle["net"] < 0]
            net = sum((cycle["net"] for cycle in selected), Decimal("0"))
            return {
                "status": coverage_status if commission_supported else "unavailable",
                "reasons": ([] if commission_supported else [
                    "commission_asset_conversion_unavailable"
                ]),
                "net_pnl_usdt": _string(net) if commission_supported else None,
                "net_return_pct": None,
                "return_status": "unavailable",
                "return_reasons": ["equity_baseline_unavailable"],
                "completed_count": len(selected),
                "win_count": len(wins) if commission_supported else None,
                "loss_count": len(losses) if commission_supported else None,
                "win_rate_pct": (
                    _string(Decimal(len(wins)) * 100 / len(selected))
                    if selected and commission_supported else None
                ),
                "payoff_ratio": (
                    _string(
                        (sum((item["net"] for item in wins), Decimal("0")) / len(wins))
                        / abs(sum((item["net"] for item in losses), Decimal("0")) / len(losses))
                    ) if wins and losses and commission_supported else None
                ),
            }

        overall = metrics(0)
        return {
            "schema_version": "1",
            "scope": {"network": scope.network, "symbol": scope.symbol, "basis": "account"},
            "as_of": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "coverage": {
                "status": coverage_status,
                "from": datetime.fromtimestamp((now_ms - SIX_MONTHS_MS) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "through": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "retention_months": 6,
                "request_count": request_count,
                "reasons": reasons,
            },
            "week": metrics(_period_start(now, "week")),
            "month": metrics(_period_start(now, "month")),
            "counts": {
                "status": coverage_status,
                "long": sum(cycle["direction"] == "LONG" for cycle in cycles),
                "short": sum(cycle["direction"] == "SHORT" for cycle in cycles),
                "completed_total": len(cycles),
            },
            "overall": overall,
        }
