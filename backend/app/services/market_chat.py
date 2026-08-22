"""Trusted market snapshots and bounded prompts for market analysis chat."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..strategies.sar_pyramid import parabolic_sar
from .ai_provider import chat_complete
from .read_only_market_gateway import ReadOnlyMarketGateway


KLINE_COLUMNS = (
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
SNAPSHOT_BAR_LIMIT = 200
HISTORY_BAR_LIMIT = 24
MAX_CONTEXT_BYTES = 18_000


class MarketDataError(RuntimeError):
    """Raised when a trustworthy, complete market snapshot cannot be built."""


@dataclass(frozen=True, slots=True)
class MarketChatResult:
    answer: str
    snapshot_at: str
    current_bar_closed_at: str
    adx_bar_closed_at: str


def _wilder_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)
    return true_range.ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()


def _iso_utc(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _closed_frame(raw: Sequence[Sequence[Any]], server_time_ms: int) -> pd.DataFrame:
    if not raw:
        raise MarketDataError("Binance did not return market data")
    try:
        frame = pd.DataFrame(raw, columns=KLINE_COLUMNS)
        frame["open_time"] = pd.to_numeric(frame["open_time"], errors="raise").astype("int64")
        frame["close_time"] = pd.to_numeric(frame["close_time"], errors="raise").astype("int64")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        raise MarketDataError("Binance returned malformed market data") from exc

    frame = frame.loc[frame["close_time"] < server_time_ms].copy()
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    prices = frame[["open", "high", "low", "close"]]
    if len(frame) < 30 or not np.isfinite(prices.to_numpy()).all():
        raise MarketDataError("Not enough completed bars for market analysis")
    if (prices <= 0).any().any() or (frame["volume"] < 0).any():
        raise MarketDataError("Binance returned invalid market data")
    if (frame["high"] < prices[["open", "low", "close"]].max(axis=1)).any() or (
        frame["low"] > prices[["open", "high", "close"]].min(axis=1)
    ).any():
        raise MarketDataError("Binance returned invalid OHLC bounds")
    return frame.reset_index(drop=True)


def _adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)
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
        {"adx": dx.ewm(**smooth).mean(), "plus_di": plus_di, "minus_di": minus_di}
    )


def _finite(value: Any, digits: int = 6) -> float | None:
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def _return(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    return _finite(close.iloc[-1] / close.iloc[-1 - bars] - 1.0)


def build_market_snapshot(
    *,
    symbol: str,
    interval: str,
    server_time_ms: int,
    current_raw: Sequence[Sequence[Any]],
    hourly_raw: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    current = _closed_frame(current_raw, server_time_ms)
    hourly = current if interval == "1h" and hourly_raw is current_raw else _closed_frame(
        hourly_raw, server_time_ms
    )
    psar = parabolic_sar(current)
    adx = _adx(hourly).dropna()
    if adx.empty:
        raise MarketDataError("Not enough completed 1h bars for ADX")

    latest = current.iloc[-1]
    previous_atr = _wilder_atr(current).shift(1).iloc[-1]
    body_size = abs(float(latest["close"]) - float(latest["open"]))
    body_atr_ratio = body_size / previous_atr if previous_atr > 0 else math.nan
    latest_psar = psar.iloc[-1]
    latest_adx = adx.iloc[-1]
    previous_adx = adx.iloc[-2] if len(adx) > 1 else latest_adx
    reversals = np.flatnonzero(psar["sar_reversal"].to_numpy())
    bars_since_reversal = int(len(current) - 1 - reversals[-1]) if len(reversals) else None
    recent = current.tail(HISTORY_BAR_LIMIT)
    base = float(recent["close"].iloc[0])
    history = [
        {
            "closed_at": _iso_utc(int(row.close_time)),
            "open": _finite(row.open / base - 1.0),
            "high": _finite(row.high / base - 1.0),
            "low": _finite(row.low / base - 1.0),
            "close": _finite(row.close / base - 1.0),
            "volume": _finite(row.volume, 3),
        }
        for row in recent.itertuples(index=False)
    ]
    snapshot = {
        "source": "Binance USD-M futures, server-fetched completed bars only",
        "symbol": symbol,
        "interval": interval,
        "snapshot_at": _iso_utc(server_time_ms),
        "current_bar_closed_at": _iso_utc(int(latest["close_time"])),
        "adx_bar_closed_at": _iso_utc(int(hourly.iloc[-1]["close_time"])),
        "price": {
            "close": _finite(latest["close"]),
            "return_1_bar": _return(current["close"], 1),
            "return_6_bars": _return(current["close"], 6),
            "return_24_bars": _return(current["close"], 24),
            "range_24_high": _finite(current.tail(24)["high"].max()),
            "range_24_low": _finite(current.tail(24)["low"].min()),
        },
        "candle": {
            "open": _finite(latest["open"]),
            "high": _finite(latest["high"]),
            "low": _finite(latest["low"]),
            "close": _finite(latest["close"]),
            "body_atr_ratio": _finite(body_atr_ratio),
            "large_body": bool(math.isfinite(body_atr_ratio) and body_atr_ratio >= 1.5),
        },
        "sar": {
            "value": _finite(latest_psar["psar"]),
            "direction": "long" if int(latest_psar["sar_direction"]) > 0 else "short",
            "reversal_on_latest_bar": bool(latest_psar["sar_reversal"]),
            "bars_since_reversal": bars_since_reversal,
        },
        "adx_1h": {
            "period": 14,
            "adx": _finite(latest_adx["adx"]),
            "adx_change": _finite(latest_adx["adx"] - previous_adx["adx"]),
            "plus_di": _finite(latest_adx["plus_di"]),
            "minus_di": _finite(latest_adx["minus_di"]),
            "direction": "long"
            if latest_adx["plus_di"] > latest_adx["minus_di"]
            else "short",
        },
        "recent_normalized_bars": history,
    }
    encoded = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise MarketDataError("Market context exceeded its safety limit")
    return snapshot


async def fetch_market_snapshot(client: Any, symbol: str, interval: str) -> dict[str, Any]:
    try:
        gateway = (
            client
            if isinstance(client, ReadOnlyMarketGateway)
            else ReadOnlyMarketGateway(client)
        )
        server_time_ms = await asyncio.to_thread(gateway.server_time)
        cutoff_ms = server_time_ms - 1
        current_raw = await asyncio.to_thread(
            gateway.klines,
            symbol=symbol,
            interval=interval,
            limit=SNAPSHOT_BAR_LIMIT,
            end_time=cutoff_ms,
        )
        hourly_raw = current_raw
        if interval != "1h":
            hourly_raw = await asyncio.to_thread(
                gateway.klines,
                symbol=symbol,
                interval="1h",
                limit=SNAPSHOT_BAR_LIMIT,
                end_time=cutoff_ms,
            )
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError("Unable to load Binance market data") from exc
    return await asyncio.to_thread(
        build_market_snapshot,
        symbol=symbol,
        interval=interval,
        server_time_ms=server_time_ms,
        current_raw=current_raw,
        hourly_raw=hourly_raw,
    )


def build_provider_messages(
    snapshot: dict[str, Any], messages: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    context = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    system = (
        "你是谨慎的加密货币期货行情研究助手。只能依据下面由服务端提供的可信快照分析，"
        "不得声称知道快照之外的实时行情，不得承诺盈利或执行交易。明确区分客观观察、"
        "可能的交易信号和不确定性；机会不足时直接说明。回答应简洁，并提醒风险。\n"
        f"TRUSTED_MARKET_SNAPSHOT={context}"
    )
    return [{"role": "system", "content": system}, *[dict(item) for item in messages]]


async def analyze_market(
    *,
    client: Any,
    symbol: str,
    interval: str,
    messages: Sequence[dict[str, str]],
    provider_config: dict[str, Any],
    proxy_url: str | None,
) -> MarketChatResult:
    snapshot = await fetch_market_snapshot(client, symbol, interval)
    answer = await chat_complete(
        provider_config["provider"],
        provider_config["api_key"],
        provider_config.get("base_url"),
        provider_config["model_name"],
        build_provider_messages(snapshot, messages),
        proxy_url,
    )
    return MarketChatResult(
        answer=answer,
        snapshot_at=snapshot["snapshot_at"],
        current_bar_closed_at=snapshot["current_bar_closed_at"],
        adx_bar_closed_at=snapshot["adx_bar_closed_at"],
    )


async def analyze_market_snapshot(
    *,
    snapshot: dict[str, Any],
    reasons: Sequence[str],
    provider_config: dict[str, Any],
    proxy_url: str | None,
) -> str:
    """Analyze one immutable completed-bar snapshot for the background agent."""

    reason_text = ", ".join(reasons)
    messages = [
        {
            "role": "user",
            "content": (
                f"This completed candle triggered: {reason_text}. Analyze the current trend, "
                "trend strength, invalidation risks, and whether this is actionable. "
                "This is read-only research; do not place orders or promise returns."
            ),
        }
    ]
    return await chat_complete(
        provider_config["provider"],
        provider_config["api_key"],
        provider_config.get("base_url"),
        provider_config["model_name"],
        build_provider_messages(snapshot, messages),
        proxy_url,
    )
