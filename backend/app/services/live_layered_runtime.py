"""Completed-bar runtime adapter for SAR layered execution strategies."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import math
from typing import Mapping

import pandas as pd

from backend.app.strategies.sar_layered import (
    LayeredPositionSnapshot,
    SarLayerMode,
    SarLayeredConfig,
    SarLayeredAction,
    SarLayeredActionType,
    SarLayeredSignal,
    SarLayeredState,
    transition_sar_layered,
)
from backend.app.strategies.sar_pyramid import BAR_INTERVAL, PositionSnapshot, parabolic_sar

from .live_strategy_runtime import DecisionPlan, LiveStrategyRuntimeError


class LiveLayeredStrategyRuntime:
    """Prepare and commit one idempotent SAR layered decision at a time."""

    def __init__(
        self,
        symbol: str,
        *,
        strategy_type: str,
        config_version: str,
        parameters: Mapping[str, object],
        restored_payload: Mapping[str, object] | None = None,
        now=None,
    ) -> None:
        mode = {
            "sar_martingale": SarLayerMode.MARTINGALE,
            "sar_anti_martingale": SarLayerMode.ANTI_MARTINGALE,
        }.get(strategy_type)
        if mode is None:
            raise ValueError(f"unsupported layered strategy: {strategy_type}")
        self.symbol = symbol.upper()
        self.strategy_type = strategy_type
        self.config_version = config_version
        self.config = SarLayeredConfig(
            mode=mode,
            sar_step=float(parameters["sar_step"]),
            sar_max=float(parameters["sar_max"]),
            max_layers=int(parameters["max_layers"]),
            multiplier=float(parameters["layer_multiplier"]),
            trigger_fraction=float(parameters["add_trigger_fraction"]),
        )
        self.config.validate()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state = SarLayeredState()
        self.last_processed_decision_time: pd.Timestamp | None = None
        self.last_execution_open_time: pd.Timestamp | None = None
        self._pending: DecisionPlan | None = None
        if restored_payload is not None:
            self._restore(restored_payload)

    @property
    def pending_plan(self) -> DecisionPlan | None:
        return self._pending

    def prepare_decision(
        self,
        bars: pd.DataFrame,
        position: PositionSnapshot,
        *,
        server_time=None,
        execution_price: float | None = None,
        eligible: bool = True,
    ) -> DecisionPlan | None:
        frame, cutoff = self._validated_tape(bars, server_time)
        sar = parabolic_sar(frame, step=self.config.sar_step, maximum=self.config.sar_max)
        tape = pd.concat([frame.reset_index(drop=True), sar.reset_index(drop=True)], axis=1)
        completed = tape.index[tape["close_time"] < cutoff].tolist()
        if not completed:
            raise LiveStrategyRuntimeError("no completed decision bar is available")
        index = completed[-1]
        decision_time = pd.Timestamp(tape.at[index, "close_time"])
        decision_id = self._decision_id(decision_time)
        if self._pending is not None:
            if self._pending.decision_id == decision_id:
                return self._pending
            raise LiveStrategyRuntimeError("a previous decision is awaiting execution confirmation")
        if self.last_processed_decision_time is not None and decision_time <= self.last_processed_decision_time:
            return None

        proposed_state = SarLayeredState(decision_time.to_pydatetime())
        if self.last_processed_decision_time is None:
            self._pending = DecisionPlan(
                decision_id, (), proposed_state, decision_time, None, None, "baseline"
            )
            return self._pending
        if decision_time != self.last_processed_decision_time + BAR_INTERVAL:
            raise LiveStrategyRuntimeError("missed an execution open; historical actions are forbidden")
        execution_index = index + 1
        if execution_index >= len(tape):
            return None
        execution_time = pd.Timestamp(tape.at[execution_index, "open_time"])
        if execution_time != decision_time + pd.Timedelta(milliseconds=1):
            raise LiveStrategyRuntimeError("decision close and next execution open are not contiguous")
        if not (execution_time <= cutoff <= pd.Timestamp(tape.at[execution_index, "close_time"])):
            raise LiveStrategyRuntimeError("execution open is no longer tradable")
        if cutoff - execution_time > pd.Timedelta(seconds=30):
            raise LiveStrategyRuntimeError("execution window expired; historical actions are forbidden")
        reference_price = float(tape.at[execution_index, "open"] if execution_price is None else execution_price)
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise LiveStrategyRuntimeError("execution price is invalid")

        if eligible:
            transition = transition_sar_layered(
                self.state,
                SarLayeredSignal(
                    bar_open_time=pd.Timestamp(tape.at[index, "open_time"]).to_pydatetime(),
                    bar_close_time=decision_time.to_pydatetime(),
                    observed_at=cutoff.to_pydatetime(),
                    decision_close=float(tape.at[index, "close"]),
                    sar_direction=int(tape.at[index, "sar_direction"]),
                    sar_reversal=bool(tape.at[index, "sar_reversal"]),
                ),
                LayeredPositionSnapshot(position.direction, position.layers, position.anchor),
                self.config,
            )
            actions = transition.actions
            proposed_state = transition.state
            reason = None if actions else "no_strategy_action"
        else:
            actions = (
                (SarLayeredAction(SarLayeredActionType.REVERSE_CLOSE, position.direction),)
                if position.direction
                else ()
            )
            reason = "symbol_not_tradable"
        self._pending = DecisionPlan(
            decision_id,
            actions,
            proposed_state,
            decision_time,
            execution_time,
            reference_price,
            reason,
        )
        return self._pending

    process_bars = prepare_decision

    def commit(self, plan: DecisionPlan | str, *, actions_confirmed: bool = True) -> None:
        decision_id = plan if isinstance(plan, str) else plan.decision_id
        if self._pending is None or self._pending.decision_id != decision_id:
            raise LiveStrategyRuntimeError("decision plan is not pending")
        if not actions_confirmed:
            raise LiveStrategyRuntimeError("exchange actions are not fully confirmed")
        pending = self._pending
        self.state = pending.proposed_state
        self.last_processed_decision_time = pending.decision_time
        self.last_execution_open_time = pending.execution_time
        self._pending = None

    def serialize_state(self, state: SarLayeredState) -> dict[str, object]:
        payload = asdict(state)
        value = payload["last_processed_close_time"]
        payload["last_processed_close_time"] = None if value is None else value.isoformat()
        return payload

    def _restore(self, payload: Mapping[str, object]) -> None:
        try:
            if payload.get("symbol", self.symbol) != self.symbol:
                raise ValueError("symbol mismatch")
            strategy = payload.get("strategy", payload.get("state"))
            if not isinstance(strategy, Mapping):
                raise TypeError("strategy state is missing")
            state_time = strategy.get("last_processed_close_time")
            parsed = None if state_time is None else pd.Timestamp(state_time).to_pydatetime()
            self.state = SarLayeredState(parsed)
            self.last_processed_decision_time = self._optional_timestamp(
                payload.get("last_processed_decision_time")
            )
            self.last_execution_open_time = self._optional_timestamp(
                payload.get("last_execution_open_time")
            )
        except (TypeError, ValueError) as exc:
            raise LiveStrategyRuntimeError("live layered strategy state is invalid") from exc

    def _decision_id(self, decision_time: pd.Timestamp) -> str:
        utc = decision_time.tz_convert("UTC")
        return f"{self.config_version}:{self.symbol}:{utc.strftime('%Y%m%dT%H%M%S%fZ')}"

    def _validated_tape(self, bars: pd.DataFrame, server_time) -> tuple[pd.DataFrame, pd.Timestamp]:
        required = {"open_time", "open", "high", "low", "close", "close_time"}
        missing = required - set(bars.columns)
        if missing:
            raise LiveStrategyRuntimeError(f"bar tape missing columns: {sorted(missing)}")
        frame = bars.loc[:, sorted(required)].copy()
        for column in ("open_time", "close_time"):
            values = frame[column]
            frame[column] = pd.to_datetime(values, unit="ms", utc=True) if pd.api.types.is_integer_dtype(values) else pd.to_datetime(values, utc=True)
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        cutoff = pd.Timestamp(server_time or self.now())
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        if len(frame) < 3:
            raise LiveStrategyRuntimeError("at least three completed bars are required")
        if frame["open_time"].duplicated().any() or not frame["open_time"].is_monotonic_increasing:
            raise LiveStrategyRuntimeError("duplicate or out-of-order bars are not allowed")
        if not frame["open_time"].diff().dropna().eq(BAR_INTERVAL).all():
            raise LiveStrategyRuntimeError("5m bar tape contains a gap or out-of-order bar")
        expected_close = frame["open_time"] + BAR_INTERVAL - pd.Timedelta(milliseconds=1)
        if not frame["close_time"].eq(expected_close).all():
            raise LiveStrategyRuntimeError("bar close timestamps are invalid")
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        if not all(math.isfinite(value) and value > 0 for value in prices.ravel()):
            raise LiveStrategyRuntimeError("bar prices must be finite and positive")
        return frame, cutoff

    @staticmethod
    def _optional_timestamp(value: object) -> pd.Timestamp | None:
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
