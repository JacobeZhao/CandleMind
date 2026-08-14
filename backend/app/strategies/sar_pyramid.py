"""Causal Parabolic-SAR reversal strategy with pullback-recapture pyramiding."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import math
from typing import Any

import numpy as np
import pandas as pd


BAR_INTERVAL = pd.Timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class SarPyramidConfig:
    initial_cash: float = 10_000.0
    target_notional_fraction: float = 1.0
    layers: int = 5
    sar_step: float = 0.02
    sar_max: float = 0.20
    fee_rate: float = 0.001
    slippage_rate: float = 0.0002
    use_adx_filter: bool = False
    adx_timeframe: str = "1h"
    adx_period: int = 14
    adx_threshold: float = 25.0
    adx_rising_periods: int = 0
    minimum_di_spread: float = 0.0
    entry_confirmation_bars: int = 0
    recapture_buffer_fraction: float = 0.0
    require_progressive_adds: bool = False
    max_entries_per_adx_regime: int = 0

    def validate(self) -> None:
        values = (
            self.initial_cash,
            self.target_notional_fraction,
            self.sar_step,
            self.sar_max,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("cash, exposure, and SAR parameters must be positive")
        if not isinstance(self.layers, int) or isinstance(self.layers, bool) or self.layers < 1:
            raise ValueError("layers must be a positive integer")
        if (
            not isinstance(self.adx_period, int)
            or isinstance(self.adx_period, bool)
            or self.adx_period < 2
        ):
            raise ValueError("adx_period must be an integer of at least two")
        _adx_timedelta(self.adx_timeframe)
        if not math.isfinite(self.adx_threshold) or not 0.0 < self.adx_threshold < 100.0:
            raise ValueError("adx_threshold must be in (0, 100)")
        for name in (
            "adx_rising_periods",
            "entry_confirmation_bars",
            "max_entries_per_adx_regime",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.minimum_di_spread) or not 0.0 <= self.minimum_di_spread < 100.0:
            raise ValueError("minimum_di_spread must be in [0, 100)")
        if (
            not math.isfinite(self.recapture_buffer_fraction)
            or not 0.0 <= self.recapture_buffer_fraction < 1.0
        ):
            raise ValueError("recapture_buffer_fraction must be in [0, 1)")
        if self.sar_step > self.sar_max:
            raise ValueError("sar_step must not exceed sar_max")
        for name in ("fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value < 1.0:
                raise ValueError(f"{name} must be in [0, 1)")


class SarPyramidActionType(str, Enum):
    OPEN = "open"
    ADD = "add"
    REVERSE_CLOSE = "reverse_close"
    TREND_FILTER_EXIT = "trend_filter_exit"
    UNIVERSE_EXIT = "universe_exit"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class SarPyramidState:
    aligned_run: int = 0
    aligned_run_direction: int = 0
    regime_direction: int = 0
    regime_entry_count: int = 0
    armed: bool = False
    rejected_add_count: int = 0


@dataclass(frozen=True, slots=True)
class SarPyramidSignal:
    sar_direction: int
    sar_reversal: bool
    trend_direction: int
    entry_trend_direction: int
    previous_trend_direction: int
    decision_close: float
    eligible: bool = True


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    direction: int = 0
    layers: int = 0
    anchor: float | None = None


@dataclass(frozen=True, slots=True)
class SarPyramidAction:
    action: SarPyramidActionType
    direction: int = 0


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: SarPyramidState
    actions: tuple[SarPyramidAction, ...]


def transition_sar_pyramid(
    state: SarPyramidState,
    signal: SarPyramidSignal,
    position: PositionSnapshot,
    execution_open: float,
    config: SarPyramidConfig,
) -> TransitionResult:
    """Evaluate one completed bar for execution at the next bar's open."""

    config.validate()
    if signal.sar_direction not in (-1, 0, 1):
        raise ValueError("sar_direction must be -1, 0, or 1")
    if signal.trend_direction not in (-1, 0, 1) or signal.entry_trend_direction not in (-1, 0, 1):
        raise ValueError("trend directions must be -1, 0, or 1")
    if position.direction not in (-1, 0, 1):
        raise ValueError("position direction must be -1, 0, or 1")
    if not math.isfinite(signal.decision_close) or signal.decision_close <= 0.0:
        raise ValueError("decision close must be finite and positive")
    if not math.isfinite(execution_open) or execution_open <= 0.0:
        raise ValueError("execution open must be finite and positive")

    if not signal.eligible:
        actions = (
            (SarPyramidAction(SarPyramidActionType.UNIVERSE_EXIT, position.direction),)
            if position.direction else ()
        )
        return TransitionResult(replace(state, armed=False), actions)

    regime_changed = signal.trend_direction != state.regime_direction
    regime_entries = 0 if regime_changed else state.regime_entry_count
    entry_aligned = (
        signal.entry_trend_direction != 0
        and signal.entry_trend_direction == signal.sar_direction
    )
    if entry_aligned:
        aligned_run = state.aligned_run + 1 if state.aligned_run_direction == signal.sar_direction else 1
        aligned_direction = signal.sar_direction
    else:
        aligned_run, aligned_direction = 0, 0
    next_state = replace(
        state,
        regime_direction=signal.trend_direction,
        regime_entry_count=regime_entries,
        aligned_run=aligned_run,
        aligned_run_direction=aligned_direction,
    )
    entry_ready = entry_aligned and aligned_run > config.entry_confirmation_bars
    capacity = config.max_entries_per_adx_regime == 0 or regime_entries < config.max_entries_per_adx_regime

    def open_action() -> tuple[SarPyramidState, SarPyramidAction]:
        return (
            replace(next_state, regime_entry_count=regime_entries + 1, armed=False),
            SarPyramidAction(SarPyramidActionType.OPEN, signal.sar_direction),
        )

    if signal.sar_reversal:
        next_state = replace(next_state, armed=False)
        actions: list[SarPyramidAction] = []
        if position.direction:
            actions.append(SarPyramidAction(SarPyramidActionType.REVERSE_CLOSE, position.direction))
        if entry_ready and capacity:
            next_state, action = open_action()
            actions.append(action)
        return TransitionResult(next_state, tuple(actions))
    if position.direction and signal.trend_direction != position.direction:
        return TransitionResult(
            replace(next_state, armed=False),
            (SarPyramidAction(SarPyramidActionType.TREND_FILTER_EXIT, position.direction),),
        )
    if (
        position.direction
        and signal.entry_trend_direction == position.direction
        and position.layers < config.layers
        and position.anchor is not None
    ):
        direction = position.direction
        below_anchor = signal.decision_close < position.anchor if direction > 0 else signal.decision_close > position.anchor
        recapture = position.anchor * (1.0 + direction * config.recapture_buffer_fraction)
        recaptured = signal.decision_close > recapture if direction > 0 else signal.decision_close < recapture
        if next_state.armed and recaptured:
            fill = execution_open * (1.0 + direction * config.slippage_rate)
            progressive = fill > position.anchor if direction > 0 else fill < position.anchor
            if not config.require_progressive_adds or progressive:
                return TransitionResult(
                    replace(next_state, armed=False),
                    (SarPyramidAction(SarPyramidActionType.ADD, direction),),
                )
            return TransitionResult(
                replace(next_state, rejected_add_count=next_state.rejected_add_count + 1),
                (),
            )
        if not next_state.armed and below_anchor:
            next_state = replace(next_state, armed=True)
        return TransitionResult(next_state, ())
    can_delayed_open = (
        not position.direction
        and config.use_adx_filter
        and entry_ready
        and capacity
        and (
            signal.trend_direction != signal.previous_trend_direction
            or config.entry_confirmation_bars > 0
            and aligned_run == config.entry_confirmation_bars + 1
        )
    )
    if can_delayed_open:
        next_state, action = open_action()
        return TransitionResult(next_state, (action,))
    return TransitionResult(next_state, ())


@dataclass(slots=True)
class _Cycle:
    cycle_id: int
    direction: int
    entry_time: pd.Timestamp
    layer_quantity: float
    entries: list[tuple[pd.Timestamp, float, float]]
    entry_fees: float = 0.0
    funding_pnl: float = 0.0

    @property
    def quantity(self) -> float:
        return sum(item[2] for item in self.entries)

    @property
    def anchor(self) -> float:
        return self.entries[-1][1]


@dataclass(frozen=True, slots=True)
class SarBacktestResult:
    symbol: str
    config: SarPyramidConfig
    metrics: dict[str, Any]
    cycles: pd.DataFrame
    fills: pd.DataFrame
    equity: pd.DataFrame
    psar: pd.DataFrame


def parabolic_sar(
    bars: pd.DataFrame, *, step: float = 0.02, maximum: float = 0.20
) -> pd.DataFrame:
    """Return causal Wilder PSAR direction and reversal state.

    The first usable state is row one. Initialization uses only rows zero and one;
    every later row depends exclusively on current/prior highs and lows.
    """

    if not math.isfinite(step) or not math.isfinite(maximum) or step <= 0 or maximum <= 0:
        raise ValueError("PSAR acceleration parameters must be positive")
    if step > maximum:
        raise ValueError("step must not exceed maximum")
    _require_columns(bars, {"high", "low", "close"})
    values = bars.loc[:, ["high", "low", "close"]].astype(float)
    if len(values) < 2:
        raise ValueError("PSAR requires at least two bars")
    if not np.isfinite(values.to_numpy()).all() or (values <= 0).any().any():
        raise ValueError("OHLC prices must be finite and positive")
    if (values["high"] < values[["low", "close"]].max(axis=1)).any() or (
        values["low"] > values[["high", "close"]].min(axis=1)
    ).any():
        raise ValueError("high/low bounds are invalid")

    high = values["high"].to_numpy()
    low = values["low"].to_numpy()
    close = values["close"].to_numpy()
    sar = np.full(len(values), np.nan)
    direction = np.zeros(len(values), dtype=np.int8)
    reversal = np.zeros(len(values), dtype=bool)

    up_move = high[1] - high[0]
    down_move = low[0] - low[1]
    bullish = up_move > down_move if up_move != down_move else close[1] >= close[0]
    direction[1] = 1 if bullish else -1
    sar[1] = min(low[0], low[1]) if bullish else max(high[0], high[1])
    extreme = max(high[0], high[1]) if bullish else min(low[0], low[1])
    acceleration = step

    for index in range(2, len(values)):
        projected = sar[index - 1] + acceleration * (extreme - sar[index - 1])
        if bullish:
            projected = min(projected, low[index - 1], low[index - 2])
            if low[index] <= projected:
                bullish = False
                reversal[index] = True
                sar[index] = extreme
                extreme = low[index]
                acceleration = step
            else:
                sar[index] = projected
                if high[index] > extreme:
                    extreme = high[index]
                    acceleration = min(maximum, acceleration + step)
        else:
            projected = max(projected, high[index - 1], high[index - 2])
            if high[index] >= projected:
                bullish = True
                reversal[index] = True
                sar[index] = extreme
                extreme = high[index]
                acceleration = step
            else:
                sar[index] = projected
                if low[index] < extreme:
                    extreme = low[index]
                    acceleration = min(maximum, acceleration + step)
        direction[index] = 1 if bullish else -1

    return pd.DataFrame(
        {"psar": sar, "sar_direction": direction, "sar_reversal": reversal},
        index=bars.index,
    )


def adx_regime(
    bars: pd.DataFrame,
    *,
    timeframe: str = "1h",
    period: int = 14,
    threshold: float = 25.0,
    rising_periods: int = 0,
    minimum_di_spread: float = 0.0,
) -> pd.DataFrame:
    """Map completed higher-timeframe ADX/+DI/-DI onto 5-minute decisions."""

    if not isinstance(period, int) or isinstance(period, bool) or period < 2:
        raise ValueError("period must be an integer of at least two")
    if not math.isfinite(threshold) or not 0.0 < threshold < 100.0:
        raise ValueError("threshold must be in (0, 100)")
    if not isinstance(rising_periods, int) or isinstance(rising_periods, bool) or rising_periods < 0:
        raise ValueError("rising_periods must be a non-negative integer")
    if not math.isfinite(minimum_di_spread) or not 0.0 <= minimum_di_spread < 100.0:
        raise ValueError("minimum_di_spread must be in [0, 100)")
    filter_interval = _adx_timedelta(timeframe)
    expected_bars = int(filter_interval / BAR_INTERVAL)
    frame = _validated_bars(bars)
    indexed = frame.set_index("open_time")
    filtered = indexed.resample(filter_interval, label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        bar_count=("close", "count"),
    )
    filtered = filtered.loc[filtered["bar_count"] == expected_bars].copy()
    previous_close = filtered["close"].shift(1)
    true_range = pd.concat(
        [
            filtered["high"] - filtered["low"],
            (filtered["high"] - previous_close).abs(),
            (filtered["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    up_move = filtered["high"].diff()
    down_move = -filtered["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0),
        index=filtered.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=filtered.index,
    )
    smooth = dict(alpha=1.0 / period, adjust=False, min_periods=period)
    atr = true_range.ewm(**smooth).mean()
    plus_di = 100.0 * plus_dm.ewm(**smooth).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(**smooth).mean() / atr
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator.where(denominator > 0.0)
    adx = dx.ewm(**smooth).mean()
    if rising_periods:
        adx_rising = adx.diff().gt(0.0).rolling(rising_periods).sum().eq(rising_periods)
    else:
        adx_rising = pd.Series(True, index=adx.index)
    di_spread = (plus_di - minus_di).abs()
    available = pd.DataFrame(
        {
            "adx_available_at": filtered.index + filter_interval,
            "adx_1h": adx.to_numpy(),
            "plus_di_1h": plus_di.to_numpy(),
            "minus_di_1h": minus_di.to_numpy(),
            "adx_rising": adx_rising.to_numpy(),
            "di_spread_1h": di_spread.to_numpy(),
        }
    ).dropna()
    available["adx_available_at"] = pd.to_datetime(
        available["adx_available_at"], utc=True
    )
    decisions = pd.DataFrame(
        {
            "decision_available_at": frame["open_time"] + BAR_INTERVAL,
            "_order": np.arange(len(frame)),
        }
    )
    mapped = pd.merge_asof(
        decisions.sort_values("decision_available_at"),
        available.sort_values("adx_available_at"),
        left_on="decision_available_at",
        right_on="adx_available_at",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_order")
    base_direction = np.where(
        mapped["adx_1h"].ge(threshold),
        np.where(mapped["plus_di_1h"] > mapped["minus_di_1h"], 1, -1),
        0,
    )
    tied = mapped["plus_di_1h"].eq(mapped["minus_di_1h"])
    base_direction = np.where(
        tied | mapped["adx_1h"].isna(), 0, base_direction
    )
    entry_allowed = mapped["adx_rising"].eq(True) & mapped["di_spread_1h"].ge(
        minimum_di_spread
    )
    entry_direction = np.where(entry_allowed, base_direction, 0)
    return pd.DataFrame(
        {
            "adx_1h": mapped["adx_1h"].to_numpy(),
            "plus_di_1h": mapped["plus_di_1h"].to_numpy(),
            "minus_di_1h": mapped["minus_di_1h"].to_numpy(),
            "adx_rising": mapped["adx_rising"].eq(True).to_numpy(dtype=bool),
            "di_spread_1h": mapped["di_spread_1h"].to_numpy(),
            "adx_available_at": mapped["adx_available_at"].to_numpy(),
            "trend_direction": base_direction.astype(np.int8),
            "entry_trend_direction": entry_direction.astype(np.int8),
        },
        index=bars.index,
    )


def hourly_adx_regime(
    bars: pd.DataFrame, *, period: int = 14, threshold: float = 25.0
) -> pd.DataFrame:
    """Backward-compatible 1-hour ADX regime helper."""

    return adx_regime(bars, timeframe="1h", period=period, threshold=threshold)


def run_sar_pyramid_backtest(
    bars: pd.DataFrame,
    *,
    symbol: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    funding: pd.DataFrame | None = None,
    eligibility: pd.DataFrame | None = None,
    config: SarPyramidConfig | None = None,
    retain_equity: bool = True,
) -> SarBacktestResult:
    """Replay one symbol with completed-bar decisions and next-open fills."""

    cfg = config or SarPyramidConfig()
    cfg.validate()
    start_at = _utc(start)
    end_at = _utc(end)
    if end_at <= start_at:
        raise ValueError("end must be after start")
    frame = _validated_bars(bars)
    sar = parabolic_sar(frame, step=cfg.sar_step, maximum=cfg.sar_max)
    if cfg.use_adx_filter:
        regime = adx_regime(
            frame,
            timeframe=cfg.adx_timeframe,
            period=cfg.adx_period,
            threshold=cfg.adx_threshold,
            rising_periods=cfg.adx_rising_periods,
            minimum_di_spread=cfg.minimum_di_spread,
        ).reset_index(drop=True)
    else:
        regime = pd.DataFrame(
            {
                "adx_1h": np.nan,
                "plus_di_1h": np.nan,
                "minus_di_1h": np.nan,
                "adx_rising": True,
                "di_spread_1h": np.nan,
                "adx_available_at": pd.NaT,
                "trend_direction": sar["sar_direction"].to_numpy(),
                "entry_trend_direction": sar["sar_direction"].to_numpy(),
            }
        )
    frame = pd.concat([frame, sar, regime], axis=1)
    funding_frame = _validated_funding(funding, symbol)
    eligibility_frame = _validated_eligibility(eligibility, symbol)

    cash = cfg.initial_cash
    cycle: _Cycle | None = None
    armed = False
    cycle_number = 0
    funding_index = 0
    fills: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    total_fees = 0.0
    total_funding = 0.0
    turnover = 0.0
    add_count = 0
    terminated_at: pd.Timestamp | None = None
    peak_equity = cfg.initial_cash
    maximum_drawdown = 0.0
    aligned_run = 0
    aligned_run_direction = 0
    rejected_add_count = 0
    regime_direction = 0
    regime_entry_count = 0
    strategy_state = SarPyramidState()

    def fill_price(reference: float, order_sign: int) -> float:
        return reference * (1.0 + order_sign * cfg.slippage_rate)

    def nav(reference: float) -> float:
        if cycle is None:
            return cash
        unrealized = sum(
            cycle.direction * quantity * (reference - price)
            for _, price, quantity in cycle.entries
        )
        return cash + unrealized

    def add_layer(at: pd.Timestamp, reference: float, reason: str) -> None:
        nonlocal cash, total_fees, turnover, add_count
        assert cycle is not None
        price = fill_price(reference, cycle.direction)
        quantity = cycle.layer_quantity
        fee = quantity * price * cfg.fee_rate
        cash -= fee
        cycle.entry_fees += fee
        cycle.entries.append((at, price, quantity))
        total_fees += fee
        turnover += quantity * price
        if reason == "add":
            add_count += 1
        fills.append(
            {
                "symbol": symbol,
                "cycle_id": cycle.cycle_id,
                "time": at,
                "action": reason,
                "direction": cycle.direction,
                "layer": len(cycle.entries),
                "reference_price": reference,
                "fill_price": price,
                "quantity": quantity,
                "fee": fee,
            }
        )

    def open_cycle(at: pd.Timestamp, reference: float, direction: int) -> bool:
        nonlocal cycle, cycle_number, armed, terminated_at, regime_entry_count
        equity = cash
        if equity <= 0.0:
            terminated_at = at
            return False
        layer_quantity = (
            equity * cfg.target_notional_fraction / reference / cfg.layers
        )
        if layer_quantity <= 0.0:
            raise RuntimeError("account equity is exhausted")
        cycle_number += 1
        cycle = _Cycle(cycle_number, direction, at, layer_quantity, [])
        armed = False
        add_layer(at, reference, "open")
        regime_entry_count += 1
        return True

    def close_cycle(at: pd.Timestamp, reference: float, reason: str) -> None:
        nonlocal cash, cycle, armed, total_fees, turnover
        assert cycle is not None
        order_sign = -cycle.direction
        price = fill_price(reference, order_sign)
        quantity = cycle.quantity
        exit_fee = quantity * price * cfg.fee_rate
        gross = sum(
            cycle.direction * item_quantity * (price - entry_price)
            for _, entry_price, item_quantity in cycle.entries
        )
        cash += gross - exit_fee
        total_fees += exit_fee
        turnover += quantity * price
        net = gross + cycle.funding_pnl - cycle.entry_fees - exit_fee
        cycles.append(
            {
                "symbol": symbol,
                "cycle_id": cycle.cycle_id,
                "direction": cycle.direction,
                "entry_time": cycle.entry_time,
                "exit_time": at,
                "exit_reason": reason,
                "layers": len(cycle.entries),
                "quantity": quantity,
                "exit_reference_price": reference,
                "exit_fill_price": price,
                "gross_price_pnl": gross,
                "funding_pnl": cycle.funding_pnl,
                "fees": cycle.entry_fees + exit_fee,
                "net_pnl": net,
            }
        )
        fills.append(
            {
                "symbol": symbol,
                "cycle_id": cycle.cycle_id,
                "time": at,
                "action": reason,
                "direction": cycle.direction,
                "layer": len(cycle.entries),
                "reference_price": reference,
                "fill_price": price,
                "quantity": quantity,
                "fee": exit_fee,
            }
        )
        cycle = None
        armed = False

    eligible = frame.index[(frame["open_time"] >= start_at) & (frame["open_time"] < end_at)]
    if len(eligible) < 2:
        raise ValueError("backtest window contains fewer than two bars")
    first_index, last_index = int(eligible[0]), int(eligible[-1])
    funding_times = funding_frame["available_at"].tolist()
    eligibility_index = 0
    eligibility_starts = eligibility_frame["effective_from"].tolist()
    eligibility_ends = eligibility_frame["effective_to"].tolist()
    eligibility_values = eligibility_frame["eligible"].tolist()

    def is_eligible(at: pd.Timestamp) -> bool:
        nonlocal eligibility_index
        if eligibility_frame.empty:
            return True
        while (
            eligibility_index < len(eligibility_ends)
            and eligibility_ends[eligibility_index] <= at
        ):
            eligibility_index += 1
        return bool(
            eligibility_index < len(eligibility_starts)
            and eligibility_starts[eligibility_index] <= at
            and at < eligibility_ends[eligibility_index]
            and eligibility_values[eligibility_index]
        )

    for index in range(max(2, first_index), last_index + 1):
        open_time = frame.at[index, "open_time"]
        open_price = float(frame.at[index, "open"])
        while funding_index < len(funding_frame) and funding_times[funding_index] <= open_time:
            if funding_times[funding_index] >= start_at and cycle is not None:
                rate = float(funding_frame.at[funding_index, "funding_rate"])
                payment = -cycle.direction * cycle.quantity * open_price * rate
                cash += payment
                cycle.funding_pnl += payment
                total_funding += payment
            funding_index += 1

        eligible_now = is_eligible(open_time)
        if not eligible_now and cycle is not None:
            close_cycle(open_time, open_price, "universe_exit")

        decision_index = index - 1
        decision_time = frame.at[decision_index, "open_time"] + BAR_INTERVAL
        if decision_time >= start_at and eligible_now and terminated_at is None:
            sar_direction = int(frame.at[decision_index, "sar_direction"])
            trend_direction = int(frame.at[decision_index, "trend_direction"])
            entry_trend_direction = int(
                frame.at[decision_index, "entry_trend_direction"]
            )
            flipped = bool(frame.at[decision_index, "sar_reversal"])
            decision_close = float(frame.at[decision_index, "close"])
            previous_trend = int(frame.at[decision_index - 1, "trend_direction"])
            position = PositionSnapshot(
                direction=0 if cycle is None else cycle.direction,
                layers=0 if cycle is None else len(cycle.entries),
                anchor=None if cycle is None else cycle.anchor,
            )
            transition = transition_sar_pyramid(
                strategy_state,
                SarPyramidSignal(
                    sar_direction=sar_direction,
                    sar_reversal=flipped,
                    trend_direction=trend_direction,
                    entry_trend_direction=entry_trend_direction,
                    previous_trend_direction=previous_trend,
                    decision_close=decision_close,
                ),
                position,
                open_price,
                cfg,
            )
            strategy_state = transition.state
            aligned_run = strategy_state.aligned_run
            aligned_run_direction = strategy_state.aligned_run_direction
            regime_direction = strategy_state.regime_direction
            regime_entry_count = strategy_state.regime_entry_count
            armed = strategy_state.armed
            rejected_add_count = strategy_state.rejected_add_count
            for action in transition.actions:
                if action.action == SarPyramidActionType.OPEN:
                    open_cycle(open_time, open_price, action.direction)
                    # open_cycle updates the legacy counter; state already owns it.
                    regime_entry_count = strategy_state.regime_entry_count
                elif action.action == SarPyramidActionType.ADD:
                    add_layer(open_time, open_price, "add")
                elif action.action in {
                    SarPyramidActionType.REVERSE_CLOSE,
                    SarPyramidActionType.TREND_FILTER_EXIT,
                }:
                    close_cycle(open_time, open_price, action.action.value)

        close_price = float(frame.at[index, "close"])
        current_equity = nav(close_price)
        peak_equity = max(peak_equity, current_equity)
        maximum_drawdown = min(maximum_drawdown, current_equity / peak_equity - 1.0)
        if retain_equity:
            equity_rows.append(
                {
                    "symbol": symbol,
                    "time": frame.at[index, "open_time"] + BAR_INTERVAL,
                    "equity": current_equity,
                    "direction": 0 if cycle is None else cycle.direction,
                    "layers": 0 if cycle is None else len(cycle.entries),
                    "armed": armed,
                }
            )

    if cycle is not None:
        final_time = frame.at[last_index, "open_time"] + BAR_INTERVAL
        close_cycle(final_time, float(frame.at[last_index, "close"]), "end_of_test")
        peak_equity = max(peak_equity, cash)
        maximum_drawdown = min(maximum_drawdown, cash / peak_equity - 1.0)
        if retain_equity:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["direction"] = 0
            equity_rows[-1]["layers"] = 0
            equity_rows[-1]["armed"] = False

    cycles_frame = pd.DataFrame(cycles)
    fills_frame = pd.DataFrame(fills)
    equity_frame = pd.DataFrame(equity_rows)
    pnls = cycles_frame["net_pnl"] if not cycles_frame.empty else pd.Series(dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    metrics = {
        "initial_cash": cfg.initial_cash,
        "final_equity": float(cash),
        "total_return": float(cash / cfg.initial_cash - 1.0),
        "max_drawdown": float(maximum_drawdown),
        "cycle_count": int(len(cycles_frame)),
        "win_rate": float((pnls > 0).mean()) if len(pnls) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "net_pnl": float(pnls.sum()),
        "fees": float(total_fees),
        "funding_pnl": float(total_funding),
        "turnover": float(turnover),
        "add_count": int(add_count),
        "average_layers": float(cycles_frame["layers"].mean()) if len(cycles_frame) else 0.0,
        "five_layer_cycle_count": int((cycles_frame["layers"] == cfg.layers).sum()) if len(cycles_frame) else 0,
        "rejected_add_count": int(rejected_add_count),
        "account_exhausted": terminated_at is not None,
        "terminated_at": None if terminated_at is None else terminated_at.isoformat(),
    }
    psar_output = frame.loc[
        :,
        [
            "open_time", "psar", "sar_direction", "sar_reversal", "adx_1h",
            "plus_di_1h", "minus_di_1h", "adx_rising", "di_spread_1h",
            "adx_available_at", "trend_direction", "entry_trend_direction",
        ],
    ]
    return SarBacktestResult(
        symbol=symbol,
        config=cfg,
        metrics=metrics,
        cycles=cycles_frame,
        fills=fills_frame,
        equity=equity_frame,
        psar=psar_output,
    )


def config_payload(config: SarPyramidConfig) -> dict[str, Any]:
    return asdict(config)


def _require_columns(frame: pd.DataFrame, required: set[str]) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"frame missing required columns: {sorted(missing)}")


def _validated_bars(bars: pd.DataFrame) -> pd.DataFrame:
    _require_columns(bars, {"open_time", "open", "high", "low", "close"})
    frame = bars.loc[:, ["open_time", "open", "high", "low", "close"]].copy()
    if pd.api.types.is_integer_dtype(frame["open_time"]):
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    else:
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame = frame.sort_values("open_time").reset_index(drop=True)
    if frame["open_time"].duplicated().any() or not frame["open_time"].is_monotonic_increasing:
        raise ValueError("bars must have unique increasing open_time values")
    if len(frame) > 1 and frame["open_time"].diff().iloc[1:].ne(BAR_INTERVAL).any():
        raise ValueError("bars must be a continuous 5-minute series")
    prices = frame[["open", "high", "low", "close"]].astype(float)
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("OHLC prices must be finite and positive")
    frame[["open", "high", "low", "close"]] = prices
    return frame


def _validated_funding(funding: pd.DataFrame | None, symbol: str) -> pd.DataFrame:
    if funding is None:
        return pd.DataFrame(columns=["available_at", "funding_rate"])
    _require_columns(funding, {"available_at", "funding_rate"})
    frame = funding.copy()
    if "symbol" in frame and frame["symbol"].ne(symbol).any():
        raise ValueError("funding contains another symbol")
    if pd.api.types.is_integer_dtype(frame["available_at"]):
        frame["available_at"] = pd.to_datetime(frame["available_at"], unit="ms", utc=True)
    else:
        frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True)
    frame["funding_rate"] = pd.to_numeric(frame["funding_rate"])
    if frame["available_at"].duplicated().any() or not frame["available_at"].is_monotonic_increasing:
        raise ValueError("funding events must be unique and increasing")
    if not np.isfinite(frame["funding_rate"].to_numpy()).all() or frame["funding_rate"].abs().ge(1).any():
        raise ValueError("funding rates must be finite and in (-1, 1)")
    return frame.loc[:, ["available_at", "funding_rate"]].reset_index(drop=True)


def _validated_eligibility(
    eligibility: pd.DataFrame | None, symbol: str
) -> pd.DataFrame:
    if eligibility is None:
        return pd.DataFrame(columns=["effective_from", "effective_to", "eligible"])
    _require_columns(
        eligibility, {"symbol", "effective_from", "effective_to", "eligible"}
    )
    frame = eligibility.loc[eligibility["symbol"] == symbol].copy()
    if frame.empty:
        raise ValueError(f"eligibility contains no rows for {symbol}")
    for column in ("effective_from", "effective_to"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame = frame.sort_values("effective_from").reset_index(drop=True)
    if (frame["effective_to"] <= frame["effective_from"]).any():
        raise ValueError("eligibility intervals must be positive")
    if len(frame) > 1 and (
        frame["effective_from"].iloc[1:].reset_index(drop=True)
        < frame["effective_to"].iloc[:-1].reset_index(drop=True)
    ).any():
        raise ValueError("eligibility intervals must not overlap")
    frame["eligible"] = frame["eligible"].astype(bool)
    return frame.loc[:, ["effective_from", "effective_to", "eligible"]]


def _utc(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _adx_timedelta(value: str) -> pd.Timedelta:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("adx_timeframe must be a non-empty duration")
    try:
        interval = pd.Timedelta(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("adx_timeframe must be a valid duration") from exc
    if (
        interval < BAR_INTERVAL
        or interval > pd.Timedelta(days=1)
        or interval % BAR_INTERVAL != pd.Timedelta(0)
    ):
        raise ValueError("adx_timeframe must be a 5-minute multiple from 5m to 1d")
    return interval
