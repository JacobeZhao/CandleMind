"""Deterministic paper broker for SAR/ADX execution; contains no exchange calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

from backend.app.strategies.sar_pyramid import PositionSnapshot, SarPyramidConfig


@dataclass(frozen=True, slots=True)
class PaperFill:
    action: str
    direction: int
    quantity: float
    reference_price: float
    fill_price: float
    fee: float
    decision_id: str


@dataclass(slots=True)
class PaperPosition:
    direction: int = 0
    layer_quantity: float = 0.0
    entries: list[dict[str, float]] = field(default_factory=list)

    @property
    def quantity(self) -> float:
        return sum(float(item["quantity"]) for item in self.entries)

    @property
    def anchor(self) -> float | None:
        return float(self.entries[-1]["price"]) if self.entries else None


class PaperBroker:
    """Account for paper fills only; duplicate decision IDs are rejected."""

    def __init__(self, initial_cash: float) -> None:
        if not math.isfinite(initial_cash) or initial_cash <= 0.0:
            raise ValueError("initial cash must be finite and positive")
        self.cash = float(initial_cash)
        self.realized_pnl = 0.0
        self.fees = 0.0
        self.funding_pnl = 0.0
        self.position = PaperPosition()
        self.processed_decisions: set[str] = set()
        self.processed_funding: set[str] = set()

    def snapshot(self) -> PositionSnapshot:
        return PositionSnapshot(
            direction=self.position.direction,
            layers=len(self.position.entries),
            anchor=self.position.anchor,
        )

    def equity(self, mark_price: float) -> float:
        self._price(mark_price)
        unrealized = sum(
            self.position.direction * float(item["quantity"]) * (mark_price - float(item["price"]))
            for item in self.position.entries
        )
        return self.cash + unrealized

    def open(self, direction: int, reference_price: float, decision_id: str, config: SarPyramidConfig) -> PaperFill:
        if self.position.direction:
            raise RuntimeError("cannot open while a position exists")
        self._begin(decision_id)
        self._direction(direction)
        config.validate()
        quantity = self.cash * config.target_notional_fraction / reference_price / config.layers
        self.position = PaperPosition(direction=direction, layer_quantity=quantity)
        return self._add("open", reference_price, decision_id, config)

    def add(self, reference_price: float, decision_id: str, config: SarPyramidConfig) -> PaperFill:
        if not self.position.direction:
            raise RuntimeError("cannot add without a position")
        if len(self.position.entries) >= config.layers:
            raise RuntimeError("position is already at the layer cap")
        self._begin(decision_id)
        return self._add("add", reference_price, decision_id, config)

    def close(self, action: str, reference_price: float, decision_id: str, config: SarPyramidConfig) -> PaperFill:
        if not self.position.direction:
            raise RuntimeError("cannot close without a position")
        self._begin(decision_id)
        self._price(reference_price)
        direction = self.position.direction
        fill_price = reference_price * (1.0 - direction * config.slippage_rate)
        quantity = self.position.quantity
        fee = quantity * fill_price * config.fee_rate
        gross = sum(
            direction * float(item["quantity"]) * (fill_price - float(item["price"]))
            for item in self.position.entries
        )
        self.cash += gross - fee
        self.realized_pnl += gross - fee
        self.fees += fee
        fill = PaperFill(action, direction, quantity, reference_price, fill_price, fee, decision_id)
        self.position = PaperPosition()
        return fill

    def settle_funding(self, funding_id: str, rate: float, mark_price: float) -> float:
        if funding_id in self.processed_funding:
            return 0.0
        if not math.isfinite(rate):
            raise ValueError("funding rate must be finite")
        self._price(mark_price)
        self.processed_funding.add(funding_id)
        if not self.position.direction:
            return 0.0
        payment = -self.position.direction * self.position.quantity * mark_price * rate
        self.cash += payment
        self.realized_pnl += payment
        self.funding_pnl += payment
        return payment

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fees": self.fees,
            "funding_pnl": self.funding_pnl,
            "position": asdict(self.position),
            "processed_decisions": sorted(self.processed_decisions),
            "processed_funding": sorted(self.processed_funding),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PaperBroker":
        broker = cls.__new__(cls)
        broker.cash = float(payload["cash"])
        broker.realized_pnl = float(payload.get("realized_pnl", 0.0))
        broker.fees = float(payload.get("fees", 0.0))
        broker.funding_pnl = float(payload.get("funding_pnl", 0.0))
        position = payload["position"]
        broker.position = PaperPosition(
            direction=int(position["direction"]),
            layer_quantity=float(position["layer_quantity"]),
            entries=[dict(item) for item in position["entries"]],
        )
        broker.processed_decisions = set(payload.get("processed_decisions", []))
        broker.processed_funding = set(payload.get("processed_funding", []))
        broker._validate()
        return broker

    def _add(self, action: str, reference: float, decision_id: str, config: SarPyramidConfig) -> PaperFill:
        self._price(reference)
        direction = self.position.direction
        fill = reference * (1.0 + direction * config.slippage_rate)
        quantity = self.position.layer_quantity
        fee = quantity * fill * config.fee_rate
        self.cash -= fee
        self.realized_pnl -= fee
        self.fees += fee
        self.position.entries.append({"price": fill, "quantity": quantity})
        return PaperFill(action, direction, quantity, reference, fill, fee, decision_id)

    def _begin(self, decision_id: str) -> None:
        if not decision_id or decision_id in self.processed_decisions:
            raise ValueError("decision ID is empty or already processed")
        self.processed_decisions.add(decision_id)

    def _validate(self) -> None:
        if not math.isfinite(self.cash):
            raise ValueError("invalid broker cash")
        self._direction(self.position.direction, allow_flat=True)
        if bool(self.position.direction) != bool(self.position.entries):
            raise ValueError("position direction and entries disagree")
        for item in self.position.entries:
            self._price(float(item["price"]))
            if float(item["quantity"]) <= 0.0:
                raise ValueError("invalid position quantity")

    @staticmethod
    def _price(value: float) -> None:
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("price must be finite and positive")

    @staticmethod
    def _direction(value: int, *, allow_flat: bool = False) -> None:
        allowed = (-1, 0, 1) if allow_flat else (-1, 1)
        if value not in allowed:
            raise ValueError("invalid direction")
