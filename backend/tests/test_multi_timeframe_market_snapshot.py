import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.services.multi_timeframe_market_snapshot import (
    ANALYSIS_INTERVALS,
    INTERVAL_MILLISECONDS,
    MultiTimeframeMarketDataError,
    build_multi_timeframe_snapshot,
    fetch_multi_timeframe_snapshot,
    latest_completed_cutoff,
)


def _raw_bars(interval, cutoff_ms, count=80, *, include_open_bar=True):
    duration = INTERVAL_MILLISECONDS[interval]
    latest_close = ((cutoff_ms + 1) // duration) * duration - 1
    latest_open = latest_close - duration + 1
    rows = []
    for offset in range(count - 1, -1, -1):
        open_time = latest_open - offset * duration
        price = 100 + (count - offset) * 0.1
        rows.append(
            [
                open_time,
                str(price),
                str(price + 0.8),
                str(price - 0.6),
                str(price + 0.2),
                "10",
                open_time + duration - 1,
                "1000",
                10,
                "5",
                "500",
                "0",
            ]
        )
    if include_open_bar:
        open_time = latest_open + duration
        rows.append(
            [open_time, "999", "1000", "998", "999", "1", open_time + duration - 1,
             "1", 1, "1", "1", "0"]
        )
    return rows


def _raw_by_interval(server_time_ms):
    cutoff = latest_completed_cutoff(server_time_ms)
    return {
        interval: _raw_bars(interval, cutoff)
        for interval in ANALYSIS_INTERVALS
    }


def test_builds_six_timeframes_at_one_completed_5m_cutoff():
    server_time_ms = int(
        datetime(2026, 8, 20, 0, 7, tzinfo=timezone.utc).timestamp() * 1000
    )

    snapshot = build_multi_timeframe_snapshot(
        symbol="SOLUSDT",
        server_time_ms=server_time_ms,
        raw_by_interval=_raw_by_interval(server_time_ms),
    )

    cutoff = latest_completed_cutoff(server_time_ms)
    assert snapshot["trigger_interval"] == "5m"
    assert tuple(snapshot["analysis_intervals"]) == ANALYSIS_INTERVALS
    assert tuple(snapshot["intervals"]) == ANALYSIS_INTERVALS
    assert snapshot["trigger_cutoff"] == "2026-08-20T00:04:59.999000Z"
    for interval, summary in snapshot["intervals"].items():
        closed_at = datetime.fromisoformat(summary["bar_closed_at"].replace("Z", "+00:00"))
        assert int(closed_at.timestamp() * 1000) <= cutoff
        assert {"sar", "adx", "atr_14", "returns", "realized_volatility_20"} <= summary.keys()
        assert {"value", "plus_di", "minus_di"} <= summary["adx"].keys()
        assert summary["close"] < 999


def test_fetch_requests_each_timeframe_once_and_uses_exchange_time():
    server_time_ms = int(
        datetime(2026, 8, 20, 0, 7, tzinfo=timezone.utc).timestamp() * 1000
    )
    rows = _raw_by_interval(server_time_ms)

    class Client:
        def __init__(self):
            self.calls = []

        def futures_time(self):
            return {"serverTime": server_time_ms}

        def futures_klines(self, *, symbol, interval, limit):
            self.calls.append((symbol, interval, limit))
            return rows[interval]

    client = Client()
    snapshot = asyncio.run(fetch_multi_timeframe_snapshot(client, "SOLUSDT"))

    assert {interval for _, interval, _ in client.calls} == set(ANALYSIS_INTERVALS)
    assert len(client.calls) == len(ANALYSIS_INTERVALS)
    assert snapshot["symbol"] == "SOLUSDT"


def test_rejects_a_stale_timeframe_instead_of_mixing_cutoffs():
    server_time_ms = int(
        datetime(2026, 8, 20, 0, 7, tzinfo=timezone.utc).timestamp() * 1000
    )
    rows = _raw_by_interval(server_time_ms)
    rows["1h"] = rows["1h"][:-2] + rows["1h"][-1:]

    with pytest.raises(MultiTimeframeMarketDataError, match="stale"):
        build_multi_timeframe_snapshot(
            symbol="SOLUSDT",
            server_time_ms=server_time_ms,
            raw_by_interval=rows,
        )
