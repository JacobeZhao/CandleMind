"""Deterministic strategy analytics and bounded Binance reconciliation."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic
from typing import Any

from .execution_store import ExecutionStore
from .strategy_analytics_store import SCHEMA_VERSION, StrategyAnalyticsStore, utc_ms


HISTORY_LOOKBACK_MS = 89 * 24 * 60 * 60 * 1000
INCOME_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


def _has_preserved_history(
    state: dict[str, Any] | None, start_ms: int, failure_reason: str
) -> bool:
    return bool(
        state is not None
        and state["coverage_start_ms"] is not None
        and int(state["coverage_start_ms"]) <= start_ms
        and (bool(state["complete"]) or state.get("reason") == failure_reason)
    )

def account_fingerprint(api_key: str) -> str:
    """Create a stable, non-reversible account binding without retaining credentials."""
    if not api_key:
        raise ValueError("active account credential is unavailable")
    return "sha256:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _s(value: Decimal) -> str:
    return format(value, "f")


def _period_start(now: datetime, period: str) -> int:
    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days=start.weekday())
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _coverage(rows: list[dict[str, Any]]) -> tuple[bool, list[str], dict[str, Any]]:
    by_stream = {row["stream"]: row for row in rows}
    reasons: list[str] = []
    for stream in ("fills", "income"):
        state = by_stream.get(stream)
        if state is None:
            reasons.append(f"{stream}_not_synced")
        elif not state["complete"]:
            reasons.append(state.get("reason") or f"{stream}_coverage_incomplete")
    payload = {
        "status": "complete" if not reasons else "incomplete",
        "reasons": reasons,
        "streams": {
            key: {
                "status": value["status"],
                "complete": bool(value["complete"]),
                "start_ms": value["coverage_start_ms"],
                "end_ms": value["coverage_end_ms"],
                "updated_at_ms": value["updated_at_ms"],
            }
            for key, value in by_stream.items()
        },
    }
    return not reasons, reasons, payload


def _derive_trades(
    fills: list[dict[str, Any]], income: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], Decimal, Decimal, bool, bool]:
    """Build exact flat-to-flat cycles; a reversal closes then opens a new cycle."""
    position = Decimal("0")
    active: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    total_commission = Decimal("0")
    supported_commissions = True
    for fill in fills:
        quantity = _d(fill["quantity"])
        signed = quantity if fill["side"] == "BUY" else -quantity
        commission = _d(fill["commission"])
        if commission and fill.get("commission_asset") not in {None, "", "USDT"}:
            supported_commissions = False
        pnl_remaining = _d(fill["realized_pnl"])
        total_commission += commission
        remaining = signed
        while remaining:
            if position == 0:
                active = {
                    "opened_at_ms": fill["time_ms"], "closed_at_ms": None,
                    "direction": "LONG" if remaining > 0 else "SHORT",
                    "realized_pnl": Decimal("0"), "commission": Decimal("0"),
                    "funding": Decimal("0"), "fills": 0,
                }
            same_direction = position == 0 or (position > 0) == (remaining > 0)
            if same_direction:
                piece = remaining
            else:
                close_quantity = min(abs(position), abs(remaining))
                piece = close_quantity if remaining > 0 else -close_quantity
            ratio = abs(piece) / quantity
            assert active is not None
            active["commission"] += commission * ratio
            if not same_direction:
                active["realized_pnl"] += pnl_remaining
                pnl_remaining = Decimal("0")
            elif position != 0:
                active["realized_pnl"] += pnl_remaining
                pnl_remaining = Decimal("0")
            active["fills"] += 1
            position += piece
            remaining -= piece
            if position == 0:
                active["closed_at_ms"] = fill["time_ms"]
                for event in income:
                    if (event["income_type"] == "FUNDING_FEE" and
                            active["opened_at_ms"] < event["time_ms"] <= active["closed_at_ms"]):
                        active["funding"] += _d(event["amount"])
                active["net"] = active["realized_pnl"] - active["commission"] + active["funding"]
                trades.append(active)
                active = None
    attributable_funding = sum((trade["funding"] for trade in trades), Decimal("0"))
    return trades, total_commission, attributable_funding, position != 0, supported_commissions


def _period_metrics(
    trades: list[dict[str, Any]],
    start_ms: int,
    complete: bool,
    reasons: list[str],
    *,
    net_available: bool = True,
    sample_available: bool = True,
) -> dict[str, Any]:
    selected = [trade for trade in trades if trade["closed_at_ms"] >= start_ms]
    wins = [trade for trade in selected if trade["net"] > 0]
    losses = [trade for trade in selected if trade["net"] < 0]
    net = sum((trade["net"] for trade in selected), Decimal("0"))
    metric_status = (
        "unavailable" if not sample_available
        else "complete" if complete
        else "partial"
    )
    metric_reasons = [] if complete else list(reasons)
    payoff = None
    if net_available and wins and losses:
        average_win = sum((trade["net"] for trade in wins), Decimal("0")) / len(wins)
        average_loss = abs(sum((trade["net"] for trade in losses), Decimal("0")) / len(losses))
        payoff = None if average_loss == 0 else _s(average_win / average_loss)
    run_totals: dict[str, tuple[Decimal, Decimal]] = {}
    return_available = net_available and sample_available
    allocation_available = True
    for trade in selected:
        allocation = trade["allocation"]
        if allocation <= 0:
            return_available = False
            allocation_available = False
            break
        run_id = trade["run_id"]
        previous_net, previous_allocation = run_totals.get(
            run_id, (Decimal("0"), allocation)
        )
        if previous_allocation != allocation:
            return_available = False
            allocation_available = False
            break
        run_totals[run_id] = (previous_net + trade["net"], allocation)
    return_factor = Decimal("1")
    for run_net, run_allocation in run_totals.values():
        return_factor *= Decimal("1") + run_net / run_allocation
    return_pct = (
        (return_factor - Decimal("1")) * Decimal("100")
        if return_available and (run_totals or not selected) else None
    )
    return_reasons = list(metric_reasons)
    if not allocation_available and "allocation_basis_missing" not in return_reasons:
        return_reasons.append("allocation_basis_missing")
    if not net_available and "commission_asset_conversion_unavailable" not in return_reasons:
        return_reasons.append("commission_asset_conversion_unavailable")
    return {
        "status": metric_status if net_available else "unavailable",
        "reasons": metric_reasons,
        "completed_count": len(selected) if sample_available else None,
        "win_count": len(wins) if sample_available and net_available else None,
        "loss_count": len(losses) if sample_available and net_available else None,
        "win_rate_pct": (_s(Decimal(len(wins)) / len(selected) * Decimal("100"))
                     if sample_available and net_available and selected else None),
        "net_pnl_usdt": _s(net) if sample_available and net_available else None,
        "net_return_pct": _s(return_pct) if return_pct is not None else None,
        "return_status": (
            "unavailable" if not return_available
            else "complete" if complete
            else "partial"
        ),
        "return_reasons": return_reasons,
        "payoff_ratio": payoff,
    }


class StrategyAnalyticsService:
    _locks_guard = threading.Lock()
    _locks: dict[tuple[str, str, str], threading.Lock] = {}
    _last_sync: dict[tuple[str, str, str], float] = {}

    def __init__(self, store: StrategyAnalyticsStore | None = None, *, cooldown_seconds: float = 15.0,
                 max_pages: int = 20, page_size: int = 1000) -> None:
        self.store = store or StrategyAnalyticsStore()
        self.cooldown_seconds = cooldown_seconds
        self.max_pages = max_pages
        self.page_size = min(max(page_size, 1), 1000)

    def capture_run(self, account: str, network: str, symbol: str, *, run_id: str,
                    strategy_type: str, config_version: str, allocation_equity: Any,
                    execution_store: ExecutionStore | None = None,
                    resume_existing: bool = False) -> tuple[int, str]:
        scope_id = self.store.ensure_scope(account, network, symbol)
        if execution_store is not None:
            journal = execution_store.load(network, symbol)
            if journal is not None:
                self.store.import_execution_journal(scope_id, journal)
        resumed = (
            self.store.latest_allocated_run(
                scope_id, strategy_type=strategy_type, config_version=config_version
            )
            if resume_existing else None
        )
        actual_run_id = resumed["run_id"] if resumed is not None else run_id
        if resumed is not None and _d(resumed["allocation_equity"]) != _d(allocation_equity):
            raise ValueError(
                "capital allocation cannot change while resuming an open strategy position"
            )
        if resumed is None:
            self.store.record_run(
                scope_id, actual_run_id, strategy_type=strategy_type,
                config_version=config_version, allocation_equity=allocation_equity,
            )
        return scope_id, actual_run_id

    def capture_order(self, scope_id: int, run_id: str, decision_id: str, ordinal: int,
                      *, exchange_order_id: Any, client_order_id: Any) -> None:
        self.store.record_owned_order(scope_id, run_id, decision_id, ordinal,
                                      exchange_order_id=exchange_order_id,
                                      client_order_id=client_order_id)

    def sync(self, client: Any, account: str, network: str, symbol: str, *, force: bool = False) -> dict[str, Any]:
        scope_id = self.store.ensure_scope(account, network, symbol)
        journal = ExecutionStore().load(network, symbol)
        if journal is not None:
            self.store.import_execution_journal(scope_id, journal)
        key = (account, network, symbol)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"status": "busy", "scope_id": scope_id}
        try:
            elapsed = monotonic() - self._last_sync.get(key, float("-inf"))
            if not force and elapsed < self.cooldown_seconds:
                return {"status": "cooldown", "scope_id": scope_id,
                        "retry_after_ms": int((self.cooldown_seconds - elapsed) * 1000)}
            result = self._sync_locked(client, scope_id, symbol)
            self._last_sync[key] = monotonic()
            return result
        finally:
            lock.release()

    def _sync_locked(self, client: Any, scope_id: int, symbol: str) -> dict[str, Any]:
        rows = self.store.snapshot_rows(scope_id)
        allocated_runs = [row for row in rows["runs"] if _d(row["allocation_equity"]) > 0]
        owned_run_ids = {row["run_id"] for row in rows["owned_orders"]}
        relevant_runs = [
            row for row in rows["runs"]
            if _d(row["allocation_equity"]) > 0 or row["run_id"] in owned_run_ids
        ]
        start_ms = min((row["started_at_ms"] for row in relevant_runs), default=utc_ms())
        now = utc_ms()
        effective_start_ms = max(start_ms, now - HISTORY_LOOKBACK_MS)
        counts = {"fills": 0, "income": 0}
        errors: list[str] = []
        partial = False
        state: dict[str, Any] | None = None
        try:
            state = self.store.get_sync_state(scope_id, "fills")
            preserved_history = _has_preserved_history(
                state, start_ms, "fills_sync_failed_after_complete_coverage"
            )
            retention_gap = (
                effective_start_ms > start_ms
                and not preserved_history
            )
            from_id = None if state is None or state["cursor"] is None else int(state["cursor"]) + 1
            latest_id = None if state is None or state["cursor"] is None else int(state["cursor"])
            coverage_start = (
                int(state["coverage_start_ms"])
                if state is not None and state["coverage_start_ms"] is not None
                else effective_start_ms
            )
            exhausted = False
            external_fills = self.store.has_integrity_flag(
                scope_id, "external_fills_present"
            )
            order_ids, client_ids = self.store.owned_order_ids(scope_id)
            for _ in range(self.max_pages):
                kwargs = {"symbol": symbol, "limit": self.page_size}
                if from_id is None:
                    kwargs["startTime"] = effective_start_ms
                else:
                    kwargs["fromId"] = from_id
                page = list(client.futures_account_trades(**kwargs) or [])
                owned = [
                    fill for fill in page
                    if str(fill.get("orderId")) in order_ids
                    or fill.get("clientOrderId") in client_ids
                ]
                external_fills = external_fills or len(owned) != len(page)
                if len(owned) != len(page):
                    self.store.mark_integrity_flag(scope_id, "external_fills_present")
                counts["fills"] += self.store.upsert_fills(scope_id, owned)
                if page:
                    latest_id = max(int(x["id"]) for x in page)
                    from_id = latest_id + 1
                if len(page) < self.page_size:
                    exhausted = True
                    break
            fill_complete = exhausted and not retention_gap and not external_fills
            fill_reason = (
                "history_retention_limit" if retention_gap
                else "external_fills_present" if external_fills
                else None if exhausted
                else "fills_pagination_limit"
            )
            self.store.set_sync_state(scope_id, "fills", cursor=latest_id,
                                      coverage_start_ms=coverage_start, coverage_end_ms=now,
                                      complete=fill_complete,
                                      status="complete" if fill_complete else "partial",
                                      reason=fill_reason)
            partial = partial or not fill_complete
            if fill_reason:
                errors.append(fill_reason)
            if external_fills and fill_reason != "external_fills_present":
                errors.append("external_fills_present")
        except Exception:
            errors.append("fills_sync_failed")
            external_fills = self.store.has_integrity_flag(
                scope_id, "external_fills_present"
            )
            preserved_history = _has_preserved_history(
                state, start_ms, "fills_sync_failed_after_complete_coverage"
            )
            self.store.set_sync_state(
                scope_id,
                "fills",
                cursor=None if state is None else state["cursor"],
                coverage_start_ms=(
                    effective_start_ms
                    if state is None or state["coverage_start_ms"] is None
                    else int(state["coverage_start_ms"])
                ),
                coverage_end_ms=now,
                status="error",
                reason=(
                    "external_fills_present"
                    if external_fills else
                    "fills_sync_failed_after_complete_coverage"
                    if preserved_history else "fills_sync_failed"
                ),
            )
        state = None
        try:
            state = self.store.get_sync_state(scope_id, "income")
            preserved_history = _has_preserved_history(
                state, start_ms, "income_sync_failed_after_complete_coverage"
            )
            retention_gap = (
                effective_start_ms > start_ms
                and not preserved_history
            )
            cursor = effective_start_ms if state is None or state["cursor"] is None else int(state["cursor"]) + 1
            coverage_start = (
                int(state["coverage_start_ms"])
                if state is not None and state["coverage_start_ms"] is not None
                else effective_start_ms
            )
            last_time = cursor - 1
            income_window_full = False
            for _ in range(self.max_pages):
                if cursor > now:
                    break
                window_end = min(cursor + INCOME_WINDOW_MS - 1, now)
                page = list(client.futures_income_history(
                    symbol=symbol, incomeType="FUNDING_FEE", startTime=cursor,
                    endTime=window_end, limit=self.page_size,
                ) or [])
                counts["income"] += self.store.upsert_income(scope_id, page)
                if len(page) >= self.page_size:
                    income_window_full = True
                    break
                last_time = window_end
                cursor = window_end + 1
            exhausted = cursor > now
            income_complete = exhausted and not retention_gap and not income_window_full
            income_reason = (
                "history_retention_limit" if retention_gap
                else "income_window_limit" if income_window_full
                else None if exhausted
                else "income_pagination_limit"
            )
            self.store.set_sync_state(scope_id, "income", cursor=last_time,
                                      coverage_start_ms=coverage_start, coverage_end_ms=now,
                                      complete=income_complete,
                                      status="complete" if income_complete else "partial",
                                      reason=income_reason)
            partial = partial or not income_complete
            if income_reason:
                errors.append(income_reason)
        except Exception:
            errors.append("income_sync_failed")
            preserved_history = _has_preserved_history(
                state, start_ms, "income_sync_failed_after_complete_coverage"
            )
            self.store.set_sync_state(
                scope_id,
                "income",
                cursor=None if state is None else state["cursor"],
                coverage_start_ms=(
                    effective_start_ms
                    if state is None or state["coverage_start_ms"] is None
                    else int(state["coverage_start_ms"])
                ),
                coverage_end_ms=now,
                status="error",
                reason=(
                    "income_sync_failed_after_complete_coverage"
                    if preserved_history else "income_sync_failed"
                ),
            )
        return {"status": "partial" if errors or partial else "complete", "scope_id": scope_id,
                "counts": counts, "reasons": errors}

    def snapshot(self, scope_id: int, *, as_of_ms: int | None = None) -> dict[str, Any]:
        as_of = as_of_ms or utc_ms()
        rows = self.store.snapshot_rows(scope_id)
        scope = self.store.scope_details(scope_id)
        complete, reasons, coverage = _coverage(rows["coverage"])
        sample_available = any(row["stream"] == "fills" for row in rows["coverage"])
        fill_state = next(
            (row for row in rows["coverage"] if row["stream"] == "fills"), None
        )
        fill_coverage_start = (
            int(fill_state["coverage_start_ms"])
            if fill_state is not None and fill_state["coverage_start_ms"] is not None
            else None
        )
        external_fills_present = self.store.has_integrity_flag(
            scope_id, "external_fills_present"
        )
        allocated_runs = [row for row in rows["runs"] if _d(row["allocation_equity"]) > 0]
        trades: list[dict[str, Any]] = []
        commission = Decimal("0")
        funding = Decimal("0")
        open_trade = False
        supported_commissions = True
        trades_by_run: dict[str, list[dict[str, Any]]] = {}
        allocation_basis_missing = False
        run_intervals: list[tuple[int, int]] = []
        position_baseline_unknown = False
        reliable_run_fills = False
        for run in rows["runs"]:
            run_fills = [
                fill for fill in rows["fills"] if fill.get("owner_run_id") == run["run_id"]
            ]
            if run_fills and (
                fill_coverage_start is None
                or fill_coverage_start > int(run["started_at_ms"])
            ):
                complete = False
                reasons.append("position_baseline_unknown")
                position_baseline_unknown = True
                trades_by_run[run["run_id"]] = []
                continue
            if run_fills:
                reliable_run_fills = True
            run_trades, run_commission, run_funding, run_open, run_supported = _derive_trades(
                run_fills, rows["income"]
            )
            allocation = _d(run["allocation_equity"])
            allocation_basis_missing = allocation_basis_missing or bool(
                run_fills and allocation <= 0
            )
            for trade in run_trades:
                trade["allocation"] = allocation
                trade["run_id"] = run["run_id"]
            trades_by_run[run["run_id"]] = run_trades
            trades.extend(run_trades)
            commission += run_commission
            funding += run_funding
            open_trade = open_trade or run_open
            supported_commissions = supported_commissions and run_supported
            if run_fills:
                run_intervals.append((
                    min(int(fill["time_ms"]) for fill in run_fills),
                    as_of if run_open else max(int(fill["time_ms"]) for fill in run_fills),
                ))
        run_intervals.sort()
        strategy_run_overlap = any(
            current[0] <= previous[1]
            for previous, current in zip(run_intervals, run_intervals[1:])
        )
        trades.sort(key=lambda trade: trade["closed_at_ms"])
        if not allocated_runs or allocation_basis_missing:
            complete = False
            reasons.append("allocation_basis_missing")
        if open_trade:
            complete = False
            reasons.append("open_position_costs_incomplete")
        if not supported_commissions:
            complete = False
            reasons.append("commission_asset_conversion_unavailable")
        if position_baseline_unknown and not reliable_run_fills:
            sample_available = False
        if external_fills_present:
            complete = False
            sample_available = False
            reasons.append("external_fills_present")
        if strategy_run_overlap:
            complete = False
            sample_available = False
            reasons.append("strategy_run_overlap")
        if not complete:
            coverage["status"] = "incomplete"
            coverage["reasons"] = list(dict.fromkeys(reasons))
        reasons = list(dict.fromkeys(reasons))
        now = datetime.fromtimestamp(as_of / 1000, tz=timezone.utc)
        equity_curve = []
        if rows["equity"] and len(allocated_runs) == 1:
            initial = _d(rows["equity"][0]["equity"])
            flows = Decimal("0")
            for index, point in enumerate(rows["equity"]):
                if index:
                    flows += _d(point["capital_flow"])
                profit = _d(point["equity"]) - initial - flows
                equity_curve.append({
                    "time": datetime.fromtimestamp(
                        point["time_ms"] / 1000, tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "equity_usdt": point["equity"],
                    "net_pnl_usdt": _s(profit),
                    "mark_price": point["mark_price"],
                })
        else:
            for run in allocated_runs:
                allocation = _d(run["allocation_equity"])
                equity_curve.append({
                    "time": datetime.fromtimestamp(
                        run["started_at_ms"] / 1000, tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "equity_usdt": _s(allocation),
                    "net_pnl_usdt": "0",
                    "mark_price": None,
                })
                cumulative = Decimal("0")
                for trade in trades_by_run.get(run["run_id"], []):
                    cumulative += trade["net"]
                    equity_curve.append({
                        "time": datetime.fromtimestamp(
                            trade["closed_at_ms"] / 1000, tz=timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                        "equity_usdt": _s(allocation + cumulative),
                        "net_pnl_usdt": _s(cumulative),
                        "mark_price": None,
                    })
            equity_curve.sort(key=lambda point: point["time"])
        completed_counts = {
            "status": (
                "unavailable" if not sample_available
                else "complete" if complete
                else "partial"
            ),
            "completed_total": len(trades) if sample_available else None,
            "long": (
                sum(trade["direction"] == "LONG" for trade in trades)
                if sample_available else None
            ),
            "short": (
                sum(trade["direction"] == "SHORT" for trade in trades)
                if sample_available else None
            ),
        }
        coverage_starts = [
            row["coverage_start_ms"] for row in rows["coverage"]
            if row.get("coverage_start_ms") is not None
        ]
        coverage_ends = [
            row["coverage_end_ms"] for row in rows["coverage"]
            if row.get("coverage_end_ms") is not None
        ]
        coverage.update({
            "from": (
                datetime.fromtimestamp(max(coverage_starts) / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if coverage_starts else None
            ),
            "through": (
                datetime.fromtimestamp(min(coverage_ends) / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if coverage_ends else None
            ),
            "sync_state": "synced" if complete else "partial",
        })
        week = _period_metrics(
            trades, _period_start(now, "week"), complete, reasons,
            net_available=supported_commissions,
            sample_available=sample_available,
        )
        month = _period_metrics(
            trades, _period_start(now, "month"), complete, reasons,
            net_available=supported_commissions,
            sample_available=sample_available,
        )
        overall_metrics = _period_metrics(
            trades, 0, complete, reasons, net_available=supported_commissions,
            sample_available=sample_available,
        )
        overall = {
            "status": overall_metrics["status"],
            "reasons": overall_metrics["reasons"],
            "completed_count": overall_metrics["completed_count"],
            "long": completed_counts["long"],
            "short": completed_counts["short"],
            "win_count": overall_metrics["win_count"],
            "loss_count": overall_metrics["loss_count"],
            "win_rate_pct": overall_metrics["win_rate_pct"],
            "payoff_ratio": overall_metrics["payoff_ratio"],
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": {
                "network": scope["network"],
                "symbol": scope["symbol"],
                "strategy_type": rows["runs"][-1]["strategy_type"] if rows["runs"] else None,
                "config_version": rows["runs"][-1]["config_version"] if rows["runs"] else None,
            },
            "as_of": datetime.fromtimestamp(as_of / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "coverage": coverage,
            "counts": completed_counts,
            "week": week,
            "month": month,
            "overall": overall,
            "costs": {
                "status": "complete" if complete else "incomplete",
                "complete": complete,
                "reasons": [] if complete else reasons,
                "commission_usdt": _s(commission) if complete else None,
                "funding_net_usdt": _s(funding) if complete else None,
                "total_cost_usdt": _s(commission - funding) if complete else None,
            },
            "equity_curve": equity_curve if complete else [],
        }
