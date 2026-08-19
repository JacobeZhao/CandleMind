"""Completed-bar decision runtime for exchange-backed strategy execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Callable, Mapping

import pandas as pd

from backend.app.strategies.sar_adx_config import CONFIG_VERSION, sar_adx_v3_config
from backend.app.strategies.sar_pyramid import (
    BAR_INTERVAL,
    PositionSnapshot,
    SarPyramidAction,
    SarPyramidSignal,
    SarPyramidState,
    adx_regime,
    parabolic_sar,
    transition_sar_pyramid,
)


class LiveStrategyRuntimeError(RuntimeError):
    """Raised when a live decision cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    decision_id: str
    actions: tuple[SarPyramidAction, ...]
    proposed_state: SarPyramidState
    decision_time: pd.Timestamp
    execution_time: pd.Timestamp | None
    reference_price: float | None
    no_action_reason: str | None


class LiveStrategyRuntime:
    """Prepare one current-bar execution plan and commit it after execution."""

    def __init__(
        self,
        symbol: str,
        *,
        restored_payload: Mapping[str, object] | None = None,
        load_state: Callable[[], Mapping[str, object] | None] | None = None,
        save_state: Callable[[dict[str, object]], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if restored_payload is not None and load_state is not None:
            raise ValueError("provide restored_payload or load_state, not both")
        self.symbol = symbol.upper()
        self.config = sar_adx_v3_config()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._save_state = save_state
        self.state = SarPyramidState()
        self.last_processed_decision_time: pd.Timestamp | None = None
        self.last_execution_open_time: pd.Timestamp | None = None
        self._pending: DecisionPlan | None = None
        payload = restored_payload if restored_payload is not None else load_state() if load_state else None
        if payload is not None:
            self._restore(payload)

    @property
    def pending_plan(self) -> DecisionPlan | None:
        return self._pending

    def prepare_decision(
        self,
        bars: pd.DataFrame,
        position: PositionSnapshot,
        *,
        server_time: datetime | pd.Timestamp | None = None,
        execution_price: float | None = None,
        eligible: bool = True,
    ) -> DecisionPlan | None:
        """Prepare the latest completed-bar decision without mutating committed state."""

        frame, cutoff = self._validated_tape(bars, server_time=server_time)
        tape = self._indicator_tape(frame)
        completed = tape.index[tape["close_time"] < cutoff].tolist()
        if not completed:
            raise LiveStrategyRuntimeError("no completed decision bar is available")
        decision_index = completed[-1]
        decision_time = pd.Timestamp(tape.at[decision_index, "close_time"])
        decision_id = self._decision_id(decision_time)

        if self._pending is not None:
            if self._pending.decision_id == decision_id:
                return self._pending
            raise LiveStrategyRuntimeError("a previous decision is awaiting execution confirmation")
        if self.last_processed_decision_time is not None and decision_time <= self.last_processed_decision_time:
            return None
        if self.last_processed_decision_time is None:
            self._pending = DecisionPlan(
                decision_id=decision_id,
                actions=(),
                proposed_state=self.state,
                decision_time=decision_time,
                execution_time=None,
                reference_price=None,
                no_action_reason="baseline",
            )
            return self._pending
        if decision_time != self.last_processed_decision_time + BAR_INTERVAL:
            raise LiveStrategyRuntimeError("missed an execution open; historical actions are forbidden")

        execution_index = decision_index + 1
        if execution_index >= len(tape):
            return None
        execution_time = pd.Timestamp(tape.at[execution_index, "open_time"])
        if execution_time != decision_time + pd.Timedelta(milliseconds=1):
            raise LiveStrategyRuntimeError("decision close and next execution open are not contiguous")
        if not (execution_time <= cutoff <= pd.Timestamp(tape.at[execution_index, "close_time"])):
            raise LiveStrategyRuntimeError("execution open is no longer tradable")
        if cutoff - execution_time > pd.Timedelta(seconds=30):
            raise LiveStrategyRuntimeError("execution window expired; historical actions are forbidden")
        reference_price = float(
            tape.at[execution_index, "open"] if execution_price is None else execution_price
        )
        if not math.isfinite(reference_price) or reference_price <= 0.0:
            raise LiveStrategyRuntimeError("execution price is invalid")

        signal = SarPyramidSignal(
            sar_direction=int(tape.at[decision_index, "sar_direction"]),
            sar_reversal=bool(tape.at[decision_index, "sar_reversal"]),
            trend_direction=int(tape.at[decision_index, "trend_direction"]),
            entry_trend_direction=int(tape.at[decision_index, "entry_trend_direction"]),
            previous_trend_direction=int(tape.at[decision_index - 1, "trend_direction"]),
            decision_close=float(tape.at[decision_index, "close"]),
            eligible=eligible,
        )
        result = transition_sar_pyramid(
            self.state,
            signal,
            position,
            reference_price,
            self.config,
        )
        self._pending = DecisionPlan(
            decision_id=decision_id,
            actions=result.actions,
            proposed_state=result.state,
            decision_time=decision_time,
            execution_time=execution_time,
            reference_price=reference_price,
            no_action_reason=None if result.actions else "no_strategy_action",
        )
        return self._pending

    process_bars = prepare_decision

    def commit(self, plan: DecisionPlan | str, *, actions_confirmed: bool = True) -> None:
        """Commit a pending plan only after every exchange action is confirmed."""

        decision_id = plan if isinstance(plan, str) else plan.decision_id
        if self._pending is None or self._pending.decision_id != decision_id:
            raise LiveStrategyRuntimeError("decision plan is not pending")
        if not actions_confirmed:
            raise LiveStrategyRuntimeError("exchange actions are not fully confirmed")
        pending = self._pending
        payload: dict[str, object] = {
            "config_version": CONFIG_VERSION,
            "symbol": self.symbol,
            "interval": "5m",
            "strategy": asdict(pending.proposed_state),
            "last_processed_decision_time": pending.decision_time.isoformat(),
            "last_execution_open_time": (
                None if pending.execution_time is None else pending.execution_time.isoformat()
            ),
            "updated_at": self.now().isoformat(),
        }
        if self._save_state is not None:
            self._save_state(payload)
        self.state = pending.proposed_state
        self.last_processed_decision_time = pending.decision_time
        self.last_execution_open_time = pending.execution_time
        self._pending = None

    commit_plan = commit

    def discard(self, plan: DecisionPlan | str) -> None:
        """Discard an unexecuted plan without advancing committed progress."""

        decision_id = plan if isinstance(plan, str) else plan.decision_id
        if self._pending is None or self._pending.decision_id != decision_id:
            raise LiveStrategyRuntimeError("decision plan is not pending")
        self._pending = None

    def _indicator_tape(self, frame: pd.DataFrame) -> pd.DataFrame:
        sar = parabolic_sar(frame, step=self.config.sar_step, maximum=self.config.sar_max)
        regime = adx_regime(
            frame,
            timeframe=self.config.adx_timeframe,
            period=self.config.adx_period,
            threshold=self.config.adx_threshold,
            rising_periods=self.config.adx_rising_periods,
            minimum_di_spread=self.config.minimum_di_spread,
        ).reset_index(drop=True)
        return pd.concat([frame.reset_index(drop=True), sar.reset_index(drop=True), regime], axis=1)

    def _validated_tape(
        self,
        bars: pd.DataFrame,
        *,
        server_time: datetime | pd.Timestamp | None,
    ) -> tuple[pd.DataFrame, pd.Timestamp]:
        required = {"open_time", "open", "high", "low", "close", "close_time"}
        missing = required - set(bars.columns)
        if missing:
            raise LiveStrategyRuntimeError(f"bar tape missing columns: {sorted(missing)}")
        frame = bars.loc[:, sorted(required)].copy()
        for column in ("open_time", "close_time"):
            values = frame[column]
            frame[column] = (
                pd.to_datetime(values, unit="ms", utc=True)
                if pd.api.types.is_integer_dtype(values)
                else pd.to_datetime(values, utc=True)
            )
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
        if not all(math.isfinite(value) and value > 0.0 for value in prices.ravel()):
            raise LiveStrategyRuntimeError("bar prices must be finite and positive")
        return frame, cutoff

    def _restore(self, payload: Mapping[str, object]) -> None:
        try:
            if payload.get("symbol", self.symbol) != self.symbol:
                raise ValueError("symbol mismatch")
            strategy = payload.get("strategy", payload.get("state"))
            if not isinstance(strategy, Mapping):
                raise TypeError("strategy state is missing")
            self.state = SarPyramidState(**strategy)
            decision = payload.get("last_processed_decision_time")
            execution = payload.get("last_execution_open_time")
            self.last_processed_decision_time = self._optional_timestamp(decision)
            self.last_execution_open_time = self._optional_timestamp(execution)
        except (TypeError, ValueError) as exc:
            raise LiveStrategyRuntimeError("live strategy state is invalid") from exc

    def _decision_id(self, decision_time: pd.Timestamp) -> str:
        utc = decision_time.tz_convert("UTC")
        return f"{CONFIG_VERSION}:{self.symbol}:{utc.strftime('%Y%m%dT%H%M%S%fZ')}"

    @staticmethod
    def _optional_timestamp(value: object) -> pd.Timestamp | None:
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
