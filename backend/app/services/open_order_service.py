"""Combine regular and conditional USD-M open orders."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .binance_usdm_gateway import BinanceGatewayError, BinanceUsdMGateway, ExchangeScope


def _text(value: Any, default: str = "0") -> str:
    return default if value in (None, "") else str(value)


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _regular(order: dict[str, Any]) -> dict[str, Any]:
    order_id = _text(order.get("orderId"), "unknown")
    return {
        "id": f"regular:{order_id}",
        "source": "regular",
        "orderId": order.get("orderId"),
        "algoId": None,
        "actualOrderId": order.get("orderId"),
        "clientOrderId": order.get("clientOrderId"),
        "symbol": _text(order.get("symbol"), ""),
        "side": _text(order.get("side"), ""),
        "positionSide": _text(order.get("positionSide"), "BOTH"),
        "type": _text(order.get("origType") or order.get("type"), "UNKNOWN"),
        "status": _text(order.get("status"), "UNKNOWN"),
        "origQty": _text(order.get("origQty")),
        "executedQty": _text(order.get("executedQty")),
        "price": _text(order.get("price")),
        "stopPrice": _text(order.get("stopPrice")),
        "time": _integer(order.get("time")),
        "updateTime": _integer(order.get("updateTime")),
        "reduceOnly": bool(order.get("reduceOnly", False)),
        "closePosition": bool(order.get("closePosition", False)),
        "aliases": [],
    }


def _algo(order: dict[str, Any]) -> dict[str, Any]:
    algo_id = _text(order.get("algoId"), "unknown")
    actual_id = order.get("actualOrderId") or None
    return {
        "id": f"algo:{algo_id}",
        "source": "algo",
        "orderId": actual_id,
        "algoId": order.get("algoId"),
        "actualOrderId": actual_id,
        "clientOrderId": order.get("clientAlgoId"),
        "symbol": _text(order.get("symbol"), ""),
        "side": _text(order.get("side"), ""),
        "positionSide": _text(order.get("positionSide"), "BOTH"),
        "type": _text(order.get("orderType"), "CONDITIONAL"),
        "status": _text(order.get("algoStatus"), "UNKNOWN"),
        "origQty": _text(order.get("quantity")),
        "executedQty": _text(order.get("executedQty")),
        "price": _text(order.get("price")),
        "stopPrice": _text(order.get("triggerPrice")),
        "time": _integer(order.get("createTime")),
        "updateTime": _integer(order.get("updateTime")),
        "reduceOnly": bool(order.get("reduceOnly", False)),
        "closePosition": bool(order.get("closePosition", False)),
        "aliases": [],
    }


class OpenOrderService:
    def combined(self, gateway: BinanceUsdMGateway, scope: ExchangeScope) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        failures: list[tuple[str, BinanceGatewayError]] = []
        counts = {"regular": 0, "algo": 0, "total": 0}
        sources: tuple[tuple[str, Callable[[str], list[dict[str, Any]]], Callable], ...] = (
            ("regular", gateway.open_orders, _regular),
            ("algo", gateway.open_algo_orders, _algo),
        )
        for name, fetch, normalize in sources:
            try:
                source_rows = fetch(scope.symbol)
                counts[name] = len(source_rows)
                rows.extend(normalize(item) for item in source_rows)
            except BinanceGatewayError as exc:
                failures.append((name, exc))
        if len(failures) == len(sources):
            raise failures[0][1]

        regular_by_id = {
            str(row["actualOrderId"]): row
            for row in rows
            if row["source"] == "regular" and row["actualOrderId"] is not None
        }
        deduplicated: list[dict[str, Any]] = []
        for row in rows:
            actual_id = row["actualOrderId"]
            if row["source"] == "algo" and actual_id is not None and str(actual_id) in regular_by_id:
                target = regular_by_id[str(actual_id)]
                target["source"] = "regular+algo"
                target["aliases"].append(row["id"])
                if target["stopPrice"] == "0":
                    target["stopPrice"] = row["stopPrice"]
                continue
            deduplicated.append(row)
        deduplicated.sort(key=lambda item: (item["time"] or 0, item["id"]), reverse=True)
        counts["total"] = len(deduplicated)
        return {
            "schema_version": "1",
            "scope": {"network": scope.network, "symbol": scope.symbol},
            "status": "partial" if failures else "complete",
            "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "counts": counts,
            "orders": deduplicated,
            "warnings": [f"{name}_orders_unavailable" for name, _ in failures],
        }
