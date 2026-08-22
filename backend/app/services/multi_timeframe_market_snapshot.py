"""Causal, completed-bar market snapshots for the market analysis agent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .read_only_market_gateway import ReadOnlyMarketGateway


ANALYSIS_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
TRIGGER_INTERVAL = "5m"
BAR_LIMIT = 200
MAX_SNAPSHOT_BYTES = 24_000
_KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)


class MultiTimeframeMarketDataError(RuntimeError):
    """Raised when a complete, causal multi-timeframe snapshot cannot be built."""


def _iso_utc(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def latest_completed_cutoff(server_time_ms: int, interval: str = TRIGGER_INTERVAL) -> int:
    duration = INTERVAL_MILLISECONDS[interval]
    return (server_time_ms // duration) * duration - 1


def _expected_close(cutoff_ms: int, interval: str) -> int:
    duration = INTERVAL_MILLISECONDS[interval]
    return ((cutoff_ms + 1) // duration) * duration - 1


def _finite(value: Any, digits: int = 8) -> float | None:
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def _closed_frame(
    raw: Sequence[Sequence[Any]], *, cutoff_ms: int, interval: str
) -> pd.DataFrame:
    if not raw:
        raise MultiTimeframeMarketDataError(f"No {interval} market data was returned")
    try:
        frame = pd.DataFrame(raw, columns=_KLINE_COLUMNS)
        frame["open_time"] = pd.to_numeric(frame["open_time"], errors="raise").astype("int64")
        frame["close_time"] = pd.to_numeric(frame["close_time"], errors="raise").astype("int64")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        raise MultiTimeframeMarketDataError(f"Malformed {interval} market data") from exc

    frame = (
        frame.loc[frame["close_time"] <= cutoff_ms]
        .sort_values("open_time")
        .drop_duplicates("open_time", keep="last")
        .reset_index(drop=True)
    )
    if len(frame) < 60:
        raise MultiTimeframeMarketDataError(f"Not enough completed {interval} bars")
    expected_close = _expected_close(cutoff_ms, interval)
    if int(frame.iloc[-1]["close_time"]) != expected_close:
        raise MultiTimeframeMarketDataError(f"Latest completed {interval} bar is stale")

    duration = INTERVAL_MILLISECONDS[interval]
    recent_open_times = frame["open_time"].tail(60).to_numpy()
    if len(recent_open_times) > 1 and not np.all(np.diff(recent_open_times) == duration):
        raise MultiTimeframeMarketDataError(f"Completed {interval} bars contain gaps")
    prices = frame[["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise MultiTimeframeMarketDataError(f"Invalid {interval} prices")
    if (frame["volume"] < 0).any():
        raise MultiTimeframeMarketDataError(f"Invalid {interval} volume")
    if (frame["high"] < prices[["open", "low", "close"]].max(axis=1)).any() or (
        frame["low"] > prices[["open", "high", "close"]].min(axis=1)
    ).any():
        raise MultiTimeframeMarketDataError(f"Invalid {interval} OHLC bounds")
    return frame


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)


def _adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    true_range = _true_range(frame)
    up_move = frame["high"].diff()
    down_move = -frame["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=frame.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=frame.index
    )
    smooth = {"alpha": 1.0 / period, "adjust": False, "min_periods": period}
    atr = true_range.ewm(**smooth).mean()
    plus_di = 100.0 * plus_dm.ewm(**smooth).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(**smooth).mean() / atr
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator.where(denominator > 0)
    return pd.DataFrame(
        {
            "adx": dx.ewm(**smooth).mean(),
            "plus_di": plus_di,
            "minus_di": minus_di,
            "atr": atr,
        }
    )


def _parabolic_sar(frame: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.DataFrame:
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    count = len(frame)
    values = np.empty(count, dtype=float)
    directions = np.ones(count, dtype=np.int8)
    reversals = np.zeros(count, dtype=bool)
    long = bool(close[1] >= close[0])
    extreme = high[0] if long else low[0]
    values[0] = low[0] if long else high[0]
    acceleration = step

    for index in range(1, count):
        candidate = values[index - 1] + acceleration * (extreme - values[index - 1])
        if long:
            candidate = min(candidate, low[index - 1], low[index - 2] if index > 1 else low[index - 1])
            if low[index] < candidate:
                long = False
                candidate = extreme
                extreme = low[index]
                acceleration = step
                reversals[index] = True
            elif high[index] > extreme:
                extreme = high[index]
                acceleration = min(maximum, acceleration + step)
        else:
            candidate = max(candidate, high[index - 1], high[index - 2] if index > 1 else high[index - 1])
            if high[index] > candidate:
                long = True
                candidate = extreme
                extreme = high[index]
                acceleration = step
                reversals[index] = True
            elif low[index] < extreme:
                extreme = low[index]
                acceleration = min(maximum, acceleration + step)
        values[index] = candidate
        directions[index] = 1 if long else -1
    return pd.DataFrame({"value": values, "direction": directions, "reversal": reversals})


def _return(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    return _finite(close.iloc[-1] / close.iloc[-1 - bars] - 1.0)


def _summarize_interval(frame: pd.DataFrame, interval: str) -> dict[str, Any]:
    latest = frame.iloc[-1]
    indicator = _adx(frame).dropna()
    if len(indicator) < 2:
        raise MultiTimeframeMarketDataError(f"Not enough {interval} bars for indicators")
    psar = _parabolic_sar(frame)
    latest_indicator = indicator.iloc[-1]
    previous_indicator = indicator.iloc[-2]
    latest_psar = psar.iloc[-1]
    reversals = np.flatnonzero(psar["reversal"].to_numpy())
    log_returns = np.log(frame["close"] / frame["close"].shift(1)).dropna()
    atr = float(latest_indicator["atr"])
    recent_close = frame["close"].tail(12)
    base = float(recent_close.iloc[0])
    body_atr_ratio = abs(float(latest["close"]) - float(latest["open"])) / atr if atr > 0 else math.nan
    return {
        "bar_closed_at": _iso_utc(int(latest["close_time"])),
        "close": _finite(latest["close"]),
        "returns": {"1": _return(frame["close"], 1), "6": _return(frame["close"], 6), "24": _return(frame["close"], 24)},
        "realized_volatility_20": _finite(log_returns.tail(20).std(ddof=0)),
        "atr_14": _finite(atr),
        "atr_percent": _finite(atr / float(latest["close"])) if latest["close"] else None,
        "body_atr_ratio": _finite(body_atr_ratio),
        "large_candle": bool(math.isfinite(body_atr_ratio) and body_atr_ratio >= 1.5),
        "sar": {
            "value": _finite(latest_psar["value"]),
            "direction": "long" if int(latest_psar["direction"]) > 0 else "short",
            "reversal": bool(latest_psar["reversal"]),
            "bars_since_reversal": int(len(frame) - 1 - reversals[-1]) if len(reversals) else None,
        },
        "adx": {
            "value": _finite(latest_indicator["adx"]),
            "change": _finite(latest_indicator["adx"] - previous_indicator["adx"]),
            "plus_di": _finite(latest_indicator["plus_di"]),
            "minus_di": _finite(latest_indicator["minus_di"]),
        },
        "recent_close_returns": [_finite(value / base - 1.0) for value in recent_close],
    }


def build_multi_timeframe_snapshot(
    *,
    symbol: str,
    server_time_ms: int,
    raw_by_interval: dict[str, Sequence[Sequence[Any]]],
    cutoff_ms: int | None = None,
) -> dict[str, Any]:
    effective_cutoff_ms = (
        latest_completed_cutoff(server_time_ms) if cutoff_ms is None else int(cutoff_ms)
    )
    if (effective_cutoff_ms + 1) % INTERVAL_MILLISECONDS[TRIGGER_INTERVAL] != 0:
        raise MultiTimeframeMarketDataError("Explicit cutoff is not a completed 5m boundary")
    if effective_cutoff_ms > latest_completed_cutoff(server_time_ms):
        raise MultiTimeframeMarketDataError("Explicit cutoff is later than exchange time")
    intervals = {
        interval: _summarize_interval(
            _closed_frame(
                raw_by_interval.get(interval, ()),
                cutoff_ms=effective_cutoff_ms,
                interval=interval,
            ),
            interval,
        )
        for interval in ANALYSIS_INTERVALS
    }
    five_minute = intervals[TRIGGER_INTERVAL]
    reasons = ["candle_closed"]
    if five_minute["large_candle"]:
        reasons.append("large_candle")
    if five_minute["sar"]["reversal"]:
        reasons.append("sar_reversal")
    snapshot = {
        "source": "Binance USD-M futures; completed bars aligned to one causal 5m cutoff",
        "symbol": symbol,
        "snapshot_at": _iso_utc(server_time_ms),
        "trigger_interval": TRIGGER_INTERVAL,
        "trigger_cutoff": _iso_utc(effective_cutoff_ms),
        "analysis_intervals": list(ANALYSIS_INTERVALS),
        "reasons": reasons,
        "intervals": intervals,
    }
    if len(json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode()) > MAX_SNAPSHOT_BYTES:
        raise MultiTimeframeMarketDataError("Market snapshot exceeded its safety limit")
    return snapshot


async def fetch_multi_timeframe_snapshot(
    client: Any, symbol: str, *, cutoff_ms: int | None = None
) -> dict[str, Any]:
    try:
        gateway = (
            client
            if isinstance(client, ReadOnlyMarketGateway)
            else ReadOnlyMarketGateway(client)
        )
        server_time_ms = await asyncio.to_thread(gateway.server_time)
        request_cutoff_ms = (
            cutoff_ms if cutoff_ms is not None else latest_completed_cutoff(server_time_ms)
        )
        rows = await asyncio.gather(
            *(
                asyncio.to_thread(
                    gateway.klines,
                    symbol=symbol,
                    interval=interval,
                    limit=BAR_LIMIT,
                    end_time=request_cutoff_ms,
                )
                for interval in ANALYSIS_INTERVALS
            )
        )
    except MultiTimeframeMarketDataError:
        raise
    except Exception as exc:
        raise MultiTimeframeMarketDataError("Unable to load Binance market data") from exc
    return await asyncio.to_thread(
        build_multi_timeframe_snapshot,
        symbol=symbol,
        server_time_ms=server_time_ms,
        raw_by_interval=dict(zip(ANALYSIS_INTERVALS, rows, strict=True)),
        cutoff_ms=request_cutoff_ms,
    )
