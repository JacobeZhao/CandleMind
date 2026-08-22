"""Read-only, in-process MCP tools for normalized market data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
import inspect
import json
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field


MarketReader = Callable[..., Any | Awaitable[Any]]
Symbol = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9]+$")]
NormalizedSymbol = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9]+$")]
DecimalText = Annotated[str, Field(min_length=1, max_length=64)]
TimestampText = Annotated[str, Field(min_length=1, max_length=40)]
ReasonText = Annotated[str, Field(min_length=1, max_length=64)]
Interval = Literal[
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"
]
KlineLimit = Annotated[int, Field(ge=1, le=200)]

MAX_KLINES = 200
MAX_INTERVALS = 6
MAX_RECENT_RETURNS = 24
MAX_RESULT_BYTES = 24_000
SNAPSHOT_INTERVALS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


class MarketMCPError(RuntimeError):
    """A safe market-tool error that does not disclose provider details."""


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TickerPayload(_Payload):
    symbol: NormalizedSymbol
    last_price: DecimalText
    high_24h: DecimalText
    low_24h: DecimalText
    close_time_ms: int | None


class KlinePayload(_Payload):
    open_time_ms: int
    close_time_ms: int
    open: DecimalText
    high: DecimalText
    low: DecimalText
    close: DecimalText
    volume: DecimalText


class CompletedKlinesPayload(_Payload):
    symbol: NormalizedSymbol
    interval: Interval
    count: int = Field(ge=0, le=MAX_KLINES)
    klines: list[KlinePayload] = Field(max_length=MAX_KLINES)


class ReturnsPayload(_Payload):
    one: float | None = Field(alias="1")
    six: float | None = Field(alias="6")
    twenty_four: float | None = Field(alias="24")


class SarPayload(_Payload):
    value: float | None
    direction: Literal["long", "short"]
    reversal: bool
    bars_since_reversal: int | None = Field(default=None, ge=0)


class AdxPayload(_Payload):
    value: float | None
    change: float | None
    plus_di: float | None
    minus_di: float | None


class IntervalSnapshotPayload(_Payload):
    bar_closed_at: TimestampText
    close: float | None
    returns: ReturnsPayload
    realized_volatility_20: float | None
    atr_14: float | None
    atr_percent: float | None
    body_atr_ratio: float | None
    large_candle: bool
    sar: SarPayload
    adx: AdxPayload
    recent_close_returns: list[float | None] = Field(max_length=MAX_RECENT_RETURNS)


class MultiTimeframeSnapshotPayload(_Payload):
    symbol: NormalizedSymbol
    snapshot_at: TimestampText
    trigger_interval: Literal["5m"]
    trigger_cutoff: TimestampText
    reasons: list[ReasonText] = Field(max_length=8)
    intervals: dict[str, IntervalSnapshotPayload] = Field(
        min_length=1, max_length=MAX_INTERVALS
    )


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def create_market_mcp_server(
    *,
    ticker_reader: MarketReader,
    completed_klines_reader: MarketReader,
    multi_timeframe_snapshot_reader: MarketReader,
) -> MCPServer:
    """Create an MCP server backed only by explicitly injected read dependencies."""

    server = MCPServer(
        "candlemind-market",
        instructions="Read-only completed market data. No account or trading capabilities.",
    )

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    async def get_ticker(symbol: Symbol) -> TickerPayload:
        """Return a normalized ticker without raw provider fields."""
        try:
            normalized_symbol = _normalize_symbol(symbol)
            raw = await _read(ticker_reader, normalized_symbol)
            return _normalize_ticker(raw, normalized_symbol)
        except MarketMCPError:
            raise
        except Exception as exc:
            raise MarketMCPError("Market data is malformed") from exc

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    async def get_completed_klines(
        symbol: Symbol, interval: Interval = "5m", limit: KlineLimit = 100
    ) -> CompletedKlinesPayload:
        """Return at most 200 completed OHLCV bars in chronological order."""
        try:
            normalized_symbol = _normalize_symbol(symbol)
            raw = await _read(completed_klines_reader, normalized_symbol, interval, limit)
            return _normalize_klines(raw, normalized_symbol, interval, limit)
        except MarketMCPError:
            raise
        except Exception as exc:
            raise MarketMCPError("Market data is malformed") from exc

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    async def get_multi_timeframe_snapshot(symbol: Symbol) -> MultiTimeframeSnapshotPayload:
        """Return a bounded, completed-bar snapshot across analysis timeframes."""
        try:
            normalized_symbol = _normalize_symbol(symbol)
            raw = await _read(multi_timeframe_snapshot_reader, normalized_symbol)
            return _normalize_snapshot(raw, normalized_symbol)
        except MarketMCPError:
            raise
        except Exception as exc:
            raise MarketMCPError("Market snapshot is malformed") from exc

    return server


async def _read(reader: MarketReader, *args: Any) -> Any:
    try:
        call = reader.__call__ if not inspect.isfunction(reader) else reader
        if inspect.iscoroutinefunction(call):
            return await reader(*args)
        result = await asyncio.to_thread(reader, *args)
        if inspect.isawaitable(result):
            return await result
        return result
    except MarketMCPError:
        raise
    except Exception as exc:
        raise MarketMCPError("Market data is unavailable") from exc


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol or len(symbol) > 32 or not symbol.isalnum():
        raise MarketMCPError("Invalid market symbol")
    return symbol


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        if name not in value:
            raise MarketMCPError("Market data is malformed")
        return value[name]
    try:
        return getattr(value, name)
    except AttributeError as exc:
        raise MarketMCPError("Market data is malformed") from exc


def _optional_field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _integer(value: Any, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise MarketMCPError("Market data is malformed")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketMCPError("Market data is malformed") from exc
    if number < 0:
        raise MarketMCPError("Market data is malformed")
    return number


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise MarketMCPError("Market data is malformed")
    return value


def _decimal(value: Any, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketMCPError("Market data is malformed") from exc
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise MarketMCPError("Market data is malformed")
    rendered = format(number, "f")
    if len(rendered) > 64:
        raise MarketMCPError("Market data is malformed")
    return number


def _decimal_string(value: Any, **constraints: bool) -> str:
    return format(_decimal(value, **constraints), "f")


def _normalize_ticker(raw: Any, symbol: str) -> TickerPayload:
    actual_symbol = _normalize_symbol(str(_field(raw, "symbol")))
    if actual_symbol != symbol:
        raise MarketMCPError("Market data symbol does not match the request")
    last = _decimal(_field(raw, "last_price"), positive=True)
    high = _decimal(_field(raw, "high_24h"), positive=True)
    low = _decimal(_field(raw, "low_24h"), positive=True)
    if low > high or not low <= last <= high:
        raise MarketMCPError("Market ticker bounds are invalid")
    return TickerPayload(
        symbol=symbol,
        last_price=format(last, "f"),
        high_24h=format(high, "f"),
        low_24h=format(low, "f"),
        close_time_ms=_integer(_field(raw, "close_time_ms"), optional=True),
    )


def _normalize_klines(
    raw: Any, symbol: str, interval: str, requested_limit: int
) -> CompletedKlinesPayload:
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        raise MarketMCPError("Market data is malformed")
    if len(raw) > requested_limit or len(raw) > MAX_KLINES:
        raise MarketMCPError("Market data exceeded the requested limit")

    normalized: list[KlinePayload] = []
    previous_open = -1
    for item in raw:
        if _normalize_symbol(str(_field(item, "symbol"))) != symbol:
            raise MarketMCPError("Market data symbol does not match the request")
        if str(_field(item, "interval")) != interval or _field(item, "closed") is not True:
            raise MarketMCPError("Only completed bars for the requested interval are allowed")
        open_time = _integer(_field(item, "open_time_ms"))
        close_time = _integer(_field(item, "close_time_ms"))
        assert open_time is not None and close_time is not None
        if open_time <= previous_open or close_time <= open_time:
            raise MarketMCPError("Market bars are not chronological")
        open_price = _decimal(_field(item, "open"), positive=True)
        high = _decimal(_field(item, "high"), positive=True)
        low = _decimal(_field(item, "low"), positive=True)
        close = _decimal(_field(item, "close"), positive=True)
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise MarketMCPError("Market OHLC bounds are invalid")
        normalized.append(
            KlinePayload(
                open_time_ms=open_time,
                close_time_ms=close_time,
                open=format(open_price, "f"),
                high=format(high, "f"),
                low=format(low, "f"),
                close=format(close, "f"),
                volume=_decimal_string(_field(item, "volume"), nonnegative=True),
            )
        )
        previous_open = open_time
    return CompletedKlinesPayload(
        symbol=symbol, interval=interval, count=len(normalized), klines=normalized
    )


def _finite(value: Any, *, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketMCPError("Market snapshot is malformed") from exc
    if not math.isfinite(number):
        raise MarketMCPError("Market snapshot is malformed")
    return round(number, 8)


def _normalize_snapshot(raw: Any, symbol: str) -> MultiTimeframeSnapshotPayload:
    if not isinstance(raw, Mapping):
        raise MarketMCPError("Market snapshot is malformed")
    if _normalize_symbol(str(_field(raw, "symbol"))) != symbol:
        raise MarketMCPError("Market data symbol does not match the request")
    raw_intervals = _field(raw, "intervals")
    if not isinstance(raw_intervals, Mapping) or not 1 <= len(raw_intervals) <= MAX_INTERVALS:
        raise MarketMCPError("Market snapshot intervals are invalid")
    if not set(raw_intervals).issubset(SNAPSHOT_INTERVALS):
        raise MarketMCPError("Market snapshot intervals are invalid")

    intervals = {
        str(interval): _normalize_interval_snapshot(values)
        for interval, values in raw_intervals.items()
    }
    cutoff_text = str(_field(raw, "trigger_cutoff"))
    snapshot_at_text = str(_field(raw, "snapshot_at"))
    cutoff = _timestamp(cutoff_text)
    snapshot_at = _timestamp(snapshot_at_text)
    cutoff_ms = int(cutoff.timestamp()) * 1000 + cutoff.microsecond // 1000
    if (cutoff_ms + 1) % 300_000 != 0 or snapshot_at < cutoff:
        raise MarketMCPError("Market snapshot timestamps are invalid")
    if any(
        _timestamp(interval.bar_closed_at) > cutoff for interval in intervals.values()
    ):
        raise MarketMCPError("Market snapshot contains an unclosed bar")
    reasons_raw = _field(raw, "reasons")
    if isinstance(reasons_raw, (str, bytes)) or not isinstance(reasons_raw, Sequence):
        raise MarketMCPError("Market snapshot is malformed")
    reasons = [str(reason)[:64] for reason in reasons_raw]
    if len(reasons) > 8:
        raise MarketMCPError("Market snapshot exceeded its safety limit")
    payload = MultiTimeframeSnapshotPayload(
        symbol=symbol,
        snapshot_at=snapshot_at_text,
        trigger_interval=str(_field(raw, "trigger_interval")),
        trigger_cutoff=cutoff_text,
        reasons=reasons,
        intervals=intervals,
    )
    encoded = json.dumps(
        payload.model_dump(by_alias=True), ensure_ascii=True, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise MarketMCPError("Market snapshot exceeded its safety limit")
    return payload


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MarketMCPError("Market snapshot timestamps are invalid") from exc
    if parsed.tzinfo is None:
        raise MarketMCPError("Market snapshot timestamps are invalid")
    return parsed.astimezone(timezone.utc)


def _normalize_interval_snapshot(raw: Any) -> IntervalSnapshotPayload:
    returns = _field(raw, "returns")
    sar = _field(raw, "sar")
    adx = _field(raw, "adx")
    recent = _field(raw, "recent_close_returns")
    if isinstance(recent, (str, bytes)) or not isinstance(recent, Sequence):
        raise MarketMCPError("Market snapshot is malformed")
    return IntervalSnapshotPayload(
        bar_closed_at=str(_field(raw, "bar_closed_at")),
        close=_finite(_field(raw, "close")),
        returns=ReturnsPayload(
            **{
                "1": _finite(_field(returns, "1")),
                "6": _finite(_field(returns, "6")),
                "24": _finite(_field(returns, "24")),
            }
        ),
        realized_volatility_20=_finite(_field(raw, "realized_volatility_20")),
        atr_14=_finite(_field(raw, "atr_14")),
        atr_percent=_finite(_field(raw, "atr_percent")),
        body_atr_ratio=_finite(_field(raw, "body_atr_ratio")),
        large_candle=_boolean(_field(raw, "large_candle")),
        sar=SarPayload(
            value=_finite(_field(sar, "value")),
            direction=str(_field(sar, "direction")),
            reversal=_boolean(_field(sar, "reversal")),
            bars_since_reversal=_integer(
                _optional_field(sar, "bars_since_reversal"), optional=True
            ),
        ),
        adx=AdxPayload(
            value=_finite(_field(adx, "value")),
            change=_finite(_field(adx, "change")),
            plus_di=_finite(_field(adx, "plus_di")),
            minus_di=_finite(_field(adx, "minus_di")),
        ),
        recent_close_returns=[_finite(value) for value in recent],
    )
