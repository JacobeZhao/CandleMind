"""Independent Backtrader execution adapter for the SAR/ADX pyramid strategy."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import backtrader as bt
import numpy as np
import pandas as pd

from .sar_pyramid import (
    BAR_INTERVAL,
    PositionSnapshot,
    SarPyramidActionType,
    SarPyramidConfig,
    SarPyramidSignal,
    SarPyramidState,
    adx_regime,
    parabolic_sar,
    transition_sar_pyramid,
)


class SarSignalFeed(bt.feeds.PandasData):
    lines = (
        "sar_direction",
        "sar_reversal",
        "trend_direction",
        "entry_trend_direction",
        "eligible",
        "funding_rate",
        "terminal",
    )
    params = (
        ("sar_direction", -1),
        ("sar_reversal", -1),
        ("trend_direction", -1),
        ("entry_trend_direction", -1),
        ("eligible", -1),
        ("funding_rate", -1),
        ("terminal", -1),
        ("volume", "volume"),
        ("openinterest", None),
    )


@dataclass(frozen=True, slots=True)
class BacktraderSarResult:
    metrics: dict[str, Any]
    fills: pd.DataFrame
    funding: pd.DataFrame
    trades: pd.DataFrame
    equity: pd.DataFrame


class SarAdxPyramidStrategy(bt.Strategy):
    params = (("config", None),)

    def __init__(self) -> None:
        if not isinstance(self.p.config, SarPyramidConfig):
            raise TypeError("config must be SarPyramidConfig")
        self.config = self.p.config
        self.config.validate()
        self.layers = 0
        self.anchor: float | None = None
        self.armed = False
        self.layer_quantity = 0.0
        self.aligned_run = 0
        self.aligned_run_direction = 0
        self.regime_direction = 0
        self.regime_entry_count = 0
        self.rejected_add_count = 0
        self.funding_pnl = 0.0
        self.fill_records: list[dict[str, Any]] = []
        self.funding_records: list[dict[str, Any]] = []
        self.trade_records: list[dict[str, Any]] = []
        self.equity_records: list[dict[str, Any]] = []
        self.active_trade_direction = 0
        self.active_trade_max_layers = 0
        self.active_trade_exit_reason: str | None = None
        self.closed_trade_metadata: list[dict[str, Any]] = []
        self._last_open: pd.Timestamp | None = None
        self.strategy_state = SarPyramidState()
        self._reverse_open_equity: float | None = None

    def prenext_open(self) -> None:
        self._on_open()

    def nextstart_open(self) -> None:
        self._on_open()

    def next_open(self) -> None:
        self._on_open()

    def next(self) -> None:
        self.equity_records.append(
            {
                "time": pd.Timestamp(self.data.datetime.datetime(0), tz="UTC")
                + BAR_INTERVAL,
                "equity": float(self.broker.getvalue()),
                "position_size": float(self.position.size),
            }
        )

    def _on_open(self) -> None:
        if len(self.data) < 2:
            return
        opened = pd.Timestamp(self.data.datetime.datetime(0), tz="UTC")
        if opened == self._last_open:
            return
        self._last_open = opened
        if bool(self.data.terminal[0]):
            if self.position.size:
                self._submit_close("end_of_test")
            return

        self._settle_funding(opened)
        sar_direction = int(self.data.sar_direction[-1])
        trend_direction = int(self.data.trend_direction[-1])
        entry_direction = int(self.data.entry_trend_direction[-1])
        flipped = bool(self.data.sar_reversal[-1])
        decision_close = float(self.data.close[-1])
        previous_trend = int(self.data.trend_direction[-2]) if len(self.data) >= 3 else 0
        position_direction = int(np.sign(self.position.size))
        transition = transition_sar_pyramid(
            self.strategy_state,
            SarPyramidSignal(
                sar_direction=sar_direction,
                sar_reversal=flipped,
                trend_direction=trend_direction,
                entry_trend_direction=entry_direction,
                previous_trend_direction=previous_trend,
                decision_close=decision_close,
                eligible=bool(self.data.eligible[0]),
            ),
            PositionSnapshot(
                direction=position_direction,
                layers=self.layers,
                anchor=self.anchor,
            ),
            float(self.data.open[0]),
            self.config,
        )
        self.strategy_state = transition.state
        self.aligned_run = transition.state.aligned_run
        self.aligned_run_direction = transition.state.aligned_run_direction
        self.regime_direction = transition.state.regime_direction
        self.regime_entry_count = transition.state.regime_entry_count
        self.armed = transition.state.armed
        self.rejected_add_count = transition.state.rejected_add_count
        for action in transition.actions:
            if action.action == SarPyramidActionType.OPEN:
                self._submit_open(action.direction)
            elif action.action == SarPyramidActionType.ADD:
                self._submit_add(action.direction)
            elif action.action in {
                SarPyramidActionType.REVERSE_CLOSE,
                SarPyramidActionType.TREND_FILTER_EXIT,
                SarPyramidActionType.UNIVERSE_EXIT,
            }:
                if action.action == SarPyramidActionType.REVERSE_CLOSE:
                    self._reverse_open_equity = self._equity_after_close(position_direction)
                self._submit_close(action.action.value)

    def _submit_open(self, direction: int) -> None:
        open_price = float(self.data.open[0])
        equity = (
            self._reverse_open_equity
            if self._reverse_open_equity is not None
            else float(self.broker.getvalue())
        )
        self._reverse_open_equity = None
        self.layer_quantity = (
            equity
            * self.config.target_notional_fraction
            / open_price
            / self.config.layers
        )
        if self.layer_quantity <= 0.0:
            return
        order = (
            self.buy(size=self.layer_quantity)
            if direction > 0
            else self.sell(size=self.layer_quantity)
        )
        order.addinfo(action="open", direction=direction, layer=1)
        self.armed = False

    def _equity_after_close(self, direction: int) -> float:
        reference = float(self.data.open[0])
        size = abs(float(self.position.size))
        close_fill = reference * (1.0 - direction * self.config.slippage_rate)
        slippage_loss = size * abs(reference - close_fill)
        close_fee = size * close_fill * self.config.fee_rate
        return float(self.broker.getvalue()) - slippage_loss - close_fee

    def _consider_add(self, decision_close: float, direction: int) -> None:
        if self.anchor is None:
            return
        if direction > 0:
            recapture = self.anchor * (1.0 + self.config.recapture_buffer_fraction)
            if self.armed and decision_close > recapture:
                prospective = float(self.data.open[0]) * (1.0 + self.config.slippage_rate)
                if not self.config.require_progressive_adds or prospective > self.anchor:
                    self._submit_add(direction)
                    self.armed = False
                else:
                    self.rejected_add_count += 1
            elif not self.armed and decision_close < self.anchor:
                self.armed = True
        else:
            recapture = self.anchor * (1.0 - self.config.recapture_buffer_fraction)
            if self.armed and decision_close < recapture:
                prospective = float(self.data.open[0]) * (1.0 - self.config.slippage_rate)
                if not self.config.require_progressive_adds or prospective < self.anchor:
                    self._submit_add(direction)
                    self.armed = False
                else:
                    self.rejected_add_count += 1
            elif not self.armed and decision_close > self.anchor:
                self.armed = True

    def _submit_add(self, direction: int) -> None:
        order = (
            self.buy(size=self.layer_quantity)
            if direction > 0
            else self.sell(size=self.layer_quantity)
        )
        order.addinfo(action="add", direction=direction, layer=self.layers + 1)

    def _submit_close(self, action: str) -> None:
        order = self.close()
        if order is not None:
            order.addinfo(
                action=action,
                direction=int(np.sign(self.position.size)),
                layer=self.layers,
            )

    def _settle_funding(self, opened: pd.Timestamp) -> None:
        rate = float(self.data.funding_rate[0])
        if not self.position.size or not math.isfinite(rate) or rate == 0.0:
            return
        notional = abs(float(self.position.size)) * float(self.data.open[0])
        payment = -int(np.sign(self.position.size)) * notional * rate
        self.broker.add_cash(payment)
        self.funding_pnl += payment
        self.funding_records.append(
            {"time": opened, "rate": rate, "notional": notional, "payment": payment}
        )

    def notify_order(self, order) -> None:
        if order.status != order.Completed:
            return
        action = order.info.get("action", "unknown")
        executed = order.executed
        self.fill_records.append(
            {
                "time": pd.Timestamp(bt.num2date(executed.dt), tz="UTC"),
                "action": action,
                "direction": int(order.info.get("direction", 0)),
                "layer": int(order.info.get("layer", 0)),
                "size": float(executed.size),
                "price": float(executed.price),
                "value": float(executed.value),
                "commission": float(executed.comm),
                "order_ref": int(order.ref),
            }
        )
        if action in {"open", "add"}:
            self.anchor = float(executed.price)
            self.layers = 1 if action == "open" else self.layers + 1
            if action == "open":
                self.active_trade_direction = int(order.info.get("direction", 0))
                self.active_trade_max_layers = 1
                self.active_trade_exit_reason = None
            else:
                self.active_trade_max_layers = max(
                    self.active_trade_max_layers, self.layers
                )
        else:
            self.closed_trade_metadata.append(
                {
                    "direction": self.active_trade_direction
                    or int(order.info.get("direction", 0)),
                    "max_layers": self.active_trade_max_layers
                    or int(order.info.get("layer", 0)),
                    "exit_reason": action,
                }
            )
            self.active_trade_direction = 0
            self.active_trade_max_layers = 0
            self.active_trade_exit_reason = None
            self.layers = 0
            self.anchor = None
            self.armed = False

    def notify_trade(self, trade) -> None:
        if not trade.isclosed:
            return
        metadata = (
            self.closed_trade_metadata.pop(0)
            if self.closed_trade_metadata
            else {
                "direction": self.active_trade_direction,
                "max_layers": self.active_trade_max_layers,
                "exit_reason": self.active_trade_exit_reason or "unknown",
            }
        )
        self.trade_records.append(
            {
                "entry_time": pd.Timestamp(bt.num2date(trade.dtopen), tz="UTC"),
                "exit_time": pd.Timestamp(bt.num2date(trade.dtclose), tz="UTC"),
                "bar_length": int(trade.barlen),
                "direction": metadata["direction"],
                "max_layers": metadata["max_layers"],
                "exit_reason": metadata["exit_reason"],
                "gross_pnl": float(trade.pnl),
                "net_pnl_before_funding": float(trade.pnlcomm),
            }
        )


def run_backtrader_sar_pyramid(
    signal_frame: pd.DataFrame,
    *,
    config: SarPyramidConfig,
) -> BacktraderSarResult:
    """Run one prepared signal tape through Backtrader's broker and order engine."""

    config.validate()
    frame = _validated_signal_frame(signal_frame)
    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.broker.setcash(config.initial_cash)
    cerebro.broker.setcommission(commission=config.fee_rate, leverage=10.0)
    cerebro.broker.set_slippage_perc(
        config.slippage_rate,
        slip_open=True,
        slip_match=True,
        slip_out=True,
    )
    cerebro.adddata(SarSignalFeed(dataname=frame))
    cerebro.addstrategy(SarAdxPyramidStrategy, config=config)
    strategies = cerebro.run(runonce=False, preload=True)
    strategy = strategies[0]
    fills = pd.DataFrame(strategy.fill_records)
    funding = pd.DataFrame(strategy.funding_records)
    trades = pd.DataFrame(strategy.trade_records)
    equity = pd.DataFrame(strategy.equity_records)
    final_equity = float(cerebro.broker.getvalue())
    if len(equity):
        drawdown = equity["equity"] / equity["equity"].cummax() - 1.0
        maximum_drawdown = float(drawdown.min())
    else:
        maximum_drawdown = 0.0
    trade_pnl = (
        trades["net_pnl_before_funding"] if len(trades) else pd.Series(dtype=float)
    )
    wins = trade_pnl[trade_pnl > 0.0]
    losses = trade_pnl[trade_pnl < 0.0]
    metrics = {
        "engine": "backtrader",
        "engine_version": bt.__version__,
        "initial_cash": config.initial_cash,
        "final_equity": final_equity,
        "total_return": final_equity / config.initial_cash - 1.0,
        "max_drawdown": maximum_drawdown,
        "fill_count": int(len(fills)),
        "trade_count": int(len(trades)),
        "win_rate": float((trade_pnl > 0.0).mean()) if len(trade_pnl) else 0.0,
        "profit_factor_before_funding": (
            float(wins.sum() / -losses.sum()) if len(losses) else None
        ),
        "commission": float(fills["commission"].sum()) if len(fills) else 0.0,
        "turnover": float((fills["size"].abs() * fills["price"]).sum()) if len(fills) else 0.0,
        "funding_pnl": float(strategy.funding_pnl),
        "rejected_add_count": int(strategy.rejected_add_count),
    }
    return BacktraderSarResult(
        metrics=metrics,
        fills=fills,
        funding=funding,
        trades=trades,
        equity=equity,
    )


def prepare_backtrader_signal_frame(
    bars: pd.DataFrame,
    *,
    funding: pd.DataFrame,
    eligibility: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    config: SarPyramidConfig,
) -> pd.DataFrame:
    """Prepare causal signal lines while leaving execution to Backtrader."""

    config.validate()
    source = bars.loc[:, ["open_time", "open", "high", "low", "close"]].copy()
    if pd.api.types.is_integer_dtype(source["open_time"]):
        source["open_time"] = pd.to_datetime(source["open_time"], unit="ms", utc=True)
    else:
        source["open_time"] = pd.to_datetime(source["open_time"], utc=True)
    source = source.sort_values("open_time").reset_index(drop=True)
    sar = parabolic_sar(source, step=config.sar_step, maximum=config.sar_max)
    regime = adx_regime(
        source,
        timeframe=config.adx_timeframe,
        period=config.adx_period,
        threshold=config.adx_threshold,
        rising_periods=config.adx_rising_periods,
        minimum_di_spread=config.minimum_di_spread,
    ).reset_index(drop=True)
    frame = pd.concat([source, sar, regime], axis=1)
    frame["eligible"] = _eligibility_mask(frame["open_time"], eligibility)
    frame["funding_rate"] = _funding_by_open(frame["open_time"], funding)
    frame["terminal"] = False
    start_at, end_at = _utc(start), _utc(end)
    selected = frame.loc[
        (frame["open_time"] >= start_at - BAR_INTERVAL)
        & (frame["open_time"] < end_at)
    ].copy()
    if selected.empty:
        raise ValueError("Backtrader signal window is empty")
    last = selected.iloc[-1].copy()
    last["open_time"] = end_at
    last[["open", "high", "low", "close"]] = float(selected.iloc[-1]["close"])
    last["eligible"] = False
    last["funding_rate"] = 0.0
    last["terminal"] = True
    selected = pd.concat([selected, last.to_frame().T], ignore_index=True)
    selected["open_time"] = pd.to_datetime(selected["open_time"], utc=True)
    selected.index = selected["open_time"].dt.tz_localize(None)
    selected["volume"] = 0.0
    return selected


def _eligibility_mask(times: pd.Series, eligibility: pd.DataFrame) -> np.ndarray:
    mask = np.zeros(len(times), dtype=bool)
    for row in eligibility.itertuples(index=False):
        start = _utc(row.effective_from)
        end = _utc(row.effective_to)
        if bool(row.eligible):
            mask |= ((times >= start) & (times < end)).to_numpy()
    return mask


def _funding_by_open(times: pd.Series, funding: pd.DataFrame) -> np.ndarray:
    rates = pd.Series(0.0, index=pd.DatetimeIndex(times))
    available = pd.to_datetime(funding["available_at"], unit="ms", utc=True) if pd.api.types.is_integer_dtype(funding["available_at"]) else pd.to_datetime(funding["available_at"], utc=True)
    execution_open = available.dt.ceil(BAR_INTERVAL)
    observed = pd.Series(
        pd.to_numeric(funding["funding_rate"]).to_numpy(), index=execution_open
    ).groupby(level=0).sum()
    common = rates.index.intersection(observed.index)
    rates.loc[common] = observed.loc[common]
    return rates.to_numpy()


def _validated_signal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "open", "high", "low", "close", "volume", "sar_direction",
        "sar_reversal", "trend_direction", "entry_trend_direction", "eligible",
        "funding_rate", "terminal",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"signal frame missing columns: {sorted(missing)}")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is not None:
        raise ValueError("signal frame index must be timezone-naive UTC DatetimeIndex")
    if frame.index.duplicated().any() or not frame.index.is_monotonic_increasing:
        raise ValueError("signal frame index must be unique and increasing")
    return frame.copy()


def _utc(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
