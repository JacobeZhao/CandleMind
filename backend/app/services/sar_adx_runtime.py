"""Completed-bar SAR/ADX V3 paper runtime."""

from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
from datetime import datetime, timezone
import math
from typing import Callable

import pandas as pd

from backend.app.strategies.sar_adx_config import CONFIG_VERSION, config_hash, sar_adx_v3_config
from backend.app.strategies.sar_pyramid import (
    BAR_INTERVAL,
    SarPyramidActionType,
    SarPyramidSignal,
    SarPyramidState,
    adx_regime,
    parabolic_sar,
    transition_sar_pyramid,
)

from .paper_broker import PaperBroker, PaperFill
from .sar_adx_state_store import SarAdxStateStore


class SarAdxRuntimeError(RuntimeError):
    pass


_UNSET = object()


class SarAdxPaperRuntime:
    """Processes a contiguous tape and persists after every executed decision."""

    def __init__(
        self,
        symbol: str,
        *,
        initial_cash: float = 10_000.0,
        state_store: SarAdxStateStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.config = sar_adx_v3_config(initial_cash=initial_cash)
        self.config_hash = config_hash(self.config)
        self.store = state_store or SarAdxStateStore()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state = SarPyramidState()
        self.broker = PaperBroker(initial_cash)
        self.last_processed_decision_time: pd.Timestamp | None = None
        self.last_execution_open_time: pd.Timestamp | None = None
        self.recovery_status = "new"
        self._restore()

    def process_bars(
        self,
        bars: pd.DataFrame,
        *,
        server_time: datetime | pd.Timestamp | None = None,
        funding: pd.DataFrame | None = None,
        execution_price: float | None = None,
        eligible: bool = True,
        allow_flat_rebaseline: bool = False,
    ) -> list[PaperFill]:
        """Execute the latest completed decision at the currently tradable open."""

        frame, cutoff = self._validated_tape(bars, server_time=server_time)
        sar = parabolic_sar(frame, step=self.config.sar_step, maximum=self.config.sar_max)
        regime = adx_regime(
            frame,
            timeframe=self.config.adx_timeframe,
            period=self.config.adx_period,
            threshold=self.config.adx_threshold,
            rising_periods=self.config.adx_rising_periods,
            minimum_di_spread=self.config.minimum_di_spread,
        ).reset_index(drop=True)
        tape = pd.concat([frame.reset_index(drop=True), sar.reset_index(drop=True), regime], axis=1)
        completed_indices = tape.index[tape["close_time"] < cutoff].tolist()
        if not completed_indices:
            raise SarAdxRuntimeError("no completed decision bar is available")
        decision_index = completed_indices[-1]
        decision_time = pd.Timestamp(tape.at[decision_index, "close_time"])
        if self.last_processed_decision_time is None:
            self._save(decision_time=decision_time)
            self.last_processed_decision_time = decision_time
            self.recovery_status = "ready"
            return []
        if decision_time <= self.last_processed_decision_time:
            return self._process_housekeeping(
                funding=funding,
                eligible=eligible,
                execution_price=execution_price,
                cutoff=cutoff,
                decision_time=decision_time,
            )
        if decision_time != self.last_processed_decision_time + BAR_INTERVAL:
            if allow_flat_rebaseline and not self.broker.position.direction:
                reset_state = SarPyramidState()
                self._save(
                    state=reset_state,
                    decision_time=decision_time,
                    execution_time=None,
                )
                self.state = reset_state
                self.last_processed_decision_time = decision_time
                self.last_execution_open_time = None
                self.recovery_status = "rebaselined"
                return []
            raise SarAdxRuntimeError("missed an execution open; historical fills are forbidden")
        execution_index = decision_index + 1
        if execution_index >= len(tape):
            return []
        execution_time = pd.Timestamp(tape.at[execution_index, "open_time"])
        if execution_time != decision_time + pd.Timedelta(milliseconds=1):
            raise SarAdxRuntimeError("decision close and next execution open are not contiguous")
        if not (execution_time <= cutoff <= pd.Timestamp(tape.at[execution_index, "close_time"])):
            raise SarAdxRuntimeError("execution open is no longer tradable")
        if cutoff - execution_time > pd.Timedelta(seconds=30):
            raise SarAdxRuntimeError("execution window expired; historical fills are forbidden")
        fill_reference = float(
            tape.at[execution_index, "open"] if execution_price is None else execution_price
        )
        if not math.isfinite(fill_reference) or fill_reference <= 0.0:
            raise SarAdxRuntimeError("execution price is invalid")

        proposed_state = deepcopy(self.state)
        proposed_broker = deepcopy(self.broker)
        if funding is not None and len(funding):
            self._settle_funding(proposed_broker, funding, execution_time, fill_reference)
        fills: list[PaperFill] = []
        try:
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
                proposed_state,
                signal,
                proposed_broker.snapshot(),
                fill_reference,
                self.config,
            )
            decision_id = f"{CONFIG_VERSION}:{self.symbol}:{decision_time.isoformat()}"
            for ordinal, action in enumerate(result.actions):
                action_id = f"{decision_id}:{ordinal}:{action.action.value}"
                if action.action == SarPyramidActionType.OPEN:
                    fill = proposed_broker.open(action.direction, fill_reference, action_id, self.config)
                elif action.action == SarPyramidActionType.ADD:
                    fill = proposed_broker.add(fill_reference, action_id, self.config)
                elif action.action in {
                    SarPyramidActionType.REVERSE_CLOSE,
                    SarPyramidActionType.TREND_FILTER_EXIT,
                    SarPyramidActionType.UNIVERSE_EXIT,
                }:
                    fill = proposed_broker.close(action.action.value, fill_reference, action_id, self.config)
                else:
                    continue
                fills.append(fill)
            self._save(
                state=result.state,
                broker=proposed_broker,
                decision_time=decision_time,
                execution_time=execution_time,
            )
        except Exception:
            raise
        self.state = result.state
        self.broker = proposed_broker
        self.last_processed_decision_time = decision_time
        self.last_execution_open_time = execution_time
        return fills

    def _process_housekeeping(
        self,
        *,
        funding: pd.DataFrame | None,
        eligible: bool,
        execution_price: float | None,
        cutoff: pd.Timestamp,
        decision_time: pd.Timestamp,
    ) -> list[PaperFill]:
        if execution_price is None:
            return []
        broker = deepcopy(self.broker)
        state = self.state
        before = broker.to_dict()
        if funding is not None and len(funding):
            self._settle_funding(broker, funding, cutoff, float(execution_price))
        fills: list[PaperFill] = []
        if not eligible and broker.position.direction:
            action_id = f"{CONFIG_VERSION}:{self.symbol}:eligibility:{decision_time.isoformat()}"
            fills.append(
                broker.close(
                    SarPyramidActionType.UNIVERSE_EXIT.value,
                    float(execution_price),
                    action_id,
                    self.config,
                )
            )
            state = replace(state, armed=False)
        if broker.to_dict() == before:
            return []
        self._save(
            state=state,
            broker=broker,
            decision_time=self.last_processed_decision_time,
            execution_time=self.last_execution_open_time,
        )
        self.state = state
        self.broker = broker
        return fills

    def status(self, mark_price: float) -> dict:
        position = self.broker.snapshot()
        return {
            "strategy_type": "sar_adx_pyramid",
            "config_version": CONFIG_VERSION,
            "symbol": self.symbol,
            "last_processed_bar": None if self.last_processed_decision_time is None else self.last_processed_decision_time.isoformat(),
            "direction": position.direction,
            "layers": position.layers,
            "paper_cash": self.broker.cash,
            "paper_equity": self.broker.equity(mark_price),
            "unrealized_pnl": self.broker.equity(mark_price) - self.broker.cash,
            "recovery_status": self.recovery_status,
        }

    def _validated_tape(self, bars: pd.DataFrame, *, server_time: datetime | pd.Timestamp | None) -> tuple[pd.DataFrame, pd.Timestamp]:
        required = {"open_time", "open", "high", "low", "close", "close_time"}
        missing = required - set(bars.columns)
        if missing:
            raise SarAdxRuntimeError(f"bar tape missing columns: {sorted(missing)}")
        frame = bars.loc[:, sorted(required)].copy()
        for column in ("open_time", "close_time"):
            values = frame[column]
            frame[column] = pd.to_datetime(values, unit="ms", utc=True) if pd.api.types.is_integer_dtype(values) else pd.to_datetime(values, utc=True)
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        cutoff = pd.Timestamp(server_time or self.now())
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        if len(frame) < 3:
            raise SarAdxRuntimeError("at least three completed bars are required")
        if frame["open_time"].duplicated().any() or not frame["open_time"].is_monotonic_increasing:
            raise SarAdxRuntimeError("duplicate or out-of-order bars are not allowed")
        gaps = frame["open_time"].diff().dropna()
        if not gaps.eq(BAR_INTERVAL).all():
            raise SarAdxRuntimeError("5m bar tape contains a gap or out-of-order bar")
        expected_close = frame["open_time"] + BAR_INTERVAL - pd.Timedelta(milliseconds=1)
        if not frame["close_time"].eq(expected_close).all():
            raise SarAdxRuntimeError("bar close timestamps are invalid")
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        if not all(math.isfinite(value) and value > 0.0 for value in prices.ravel()):
            raise SarAdxRuntimeError("bar prices must be finite and positive")
        return frame, cutoff

    def _settle_funding(self, broker: PaperBroker, funding: pd.DataFrame, cutoff: pd.Timestamp, mark_price: float) -> None:
        required = {"funding_time", "funding_rate"}
        if required - set(funding.columns):
            raise SarAdxRuntimeError("funding data is incomplete")
        rows = funding.copy()
        times = rows["funding_time"]
        rows["funding_time"] = (
            pd.to_datetime(times, unit="ms", utc=True)
            if pd.api.types.is_numeric_dtype(times)
            else pd.to_datetime(times, utc=True)
        )
        rows["funding_rate"] = pd.to_numeric(rows["funding_rate"], errors="raise")
        for row in rows.loc[rows["funding_time"] <= cutoff].itertuples(index=False):
            broker.settle_funding(row.funding_time.isoformat(), float(row.funding_rate), mark_price)

    def _restore(self) -> None:
        payload = self.store.load(self.symbol, config_version=CONFIG_VERSION, config_hash=self.config_hash)
        if payload is None:
            return
        try:
            self.state = SarPyramidState(**payload["strategy"])
            self.broker = PaperBroker.from_dict(payload["broker"])
            decision = payload.get("last_processed_decision_time")
            execution = payload.get("last_execution_open_time")
            self.last_processed_decision_time = pd.Timestamp(decision) if decision else None
            self.last_execution_open_time = pd.Timestamp(execution) if execution else None
        except (KeyError, TypeError, ValueError) as exc:
            raise SarAdxRuntimeError("paper state content is invalid") from exc
        self.recovery_status = "recovered"

    def _save(
        self,
        *,
        state: SarPyramidState | None = None,
        broker: PaperBroker | None = None,
        decision_time: pd.Timestamp | None = None,
        execution_time: pd.Timestamp | None | object = _UNSET,
    ) -> None:
        state = self.state if state is None else state
        broker = self.broker if broker is None else broker
        decision_time = self.last_processed_decision_time if decision_time is None else decision_time
        if execution_time is _UNSET:
            execution_time = self.last_execution_open_time
        self.store.save(
            self.symbol,
            {
                "config_version": CONFIG_VERSION,
                "config_hash": self.config_hash,
                "symbol": self.symbol,
                "interval": "5m",
                "last_processed_decision_time": None if decision_time is None else decision_time.isoformat(),
                "last_execution_open_time": None if execution_time is None else execution_time.isoformat(),
                "strategy": asdict(state),
                "broker": broker.to_dict(),
                "updated_at": self.now().isoformat(),
            },
        )
