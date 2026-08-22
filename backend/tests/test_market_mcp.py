import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal

from mcp import Client

from backend.app.services.market_mcp import create_market_mcp_server


@dataclass(frozen=True)
class Ticker:
    symbol: str = "SOLUSDT"
    last_price: Decimal = Decimal("150.25")
    high_24h: Decimal = Decimal("155")
    low_24h: Decimal = Decimal("145")
    close_time_ms: int = 2_000
    api_key: str = "must-not-leak"


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool = True
    raw: dict | None = None


def _bar(index: int, interval: str = "5m") -> Bar:
    return Bar(
        symbol="SOLUSDT",
        interval=interval,
        open_time_ms=1_000 + index * 500,
        close_time_ms=1_499 + index * 500,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("12.5"),
        raw={"secret": "must-not-leak"},
    )


def _interval_snapshot():
    return {
        "bar_closed_at": "2026-08-22T00:04:59.999Z",
        "close": 105.0,
        "returns": {"1": 0.01, "6": 0.02, "24": None},
        "realized_volatility_20": 0.03,
        "atr_14": 2.0,
        "atr_percent": 0.019,
        "body_atr_ratio": 0.5,
        "large_candle": False,
        "sar": {
            "value": 99.0,
            "direction": "long",
            "reversal": False,
            "bars_since_reversal": 3,
            "raw": "must-not-leak",
        },
        "adx": {"value": 25.0, "change": 1.0, "plus_di": 30.0, "minus_di": 15.0},
        "recent_close_returns": [0.0, 0.01],
        "provider_payload": "must-not-leak",
    }


def _snapshot(symbol: str):
    return {
        "source": "provider and internal details must not be returned",
        "symbol": symbol,
        "snapshot_at": "2026-08-22T00:05:01Z",
        "trigger_interval": "5m",
        "trigger_cutoff": "2026-08-22T00:04:59.999Z",
        "reasons": ["candle_closed"],
        "intervals": {"5m": _interval_snapshot(), "1h": _interval_snapshot()},
        "credentials": {"api_key": "must-not-leak"},
    }


def _server(*, ticker_reader=None, klines_reader=None, snapshot_reader=None):
    return create_market_mcp_server(
        ticker_reader=ticker_reader or (lambda symbol: Ticker(symbol=symbol)),
        completed_klines_reader=klines_reader
        or (
            lambda symbol, interval, limit: [
                _bar(index, interval) for index in range(min(2, limit))
            ]
        ),
        multi_timeframe_snapshot_reader=snapshot_reader or _snapshot,
    )


def test_in_process_client_lists_only_bounded_read_only_market_tools():
    async def exercise():
        async with Client(_server()) as client:
            return await client.list_tools()

    listing = asyncio.run(exercise())
    tools = {tool.name: tool for tool in listing.tools}

    assert set(tools) == {
        "get_ticker",
        "get_completed_klines",
        "get_multi_timeframe_snapshot",
    }
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
    kline_schema = tools["get_completed_klines"].input_schema
    assert kline_schema["properties"]["limit"]["maximum"] == 200
    assert kline_schema["properties"]["limit"]["minimum"] == 1
    assert set(kline_schema["properties"]["interval"]["enum"]) == {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"
    }
    output_schema = tools["get_completed_klines"].output_schema
    assert output_schema["properties"]["klines"]["maxItems"] == 200
    close_schema = output_schema["$defs"]["KlinePayload"]["properties"]["close"]
    assert close_schema["maxLength"] == 64


def test_tools_normalize_outputs_and_drop_unapproved_provider_fields():
    async def exercise():
        async with Client(_server()) as client:
            ticker = await client.call_tool("get_ticker", {"symbol": "solusdt"})
            klines = await client.call_tool(
                "get_completed_klines", {"symbol": "SOLUSDT", "interval": "5m", "limit": 2}
            )
            snapshot = await client.call_tool(
                "get_multi_timeframe_snapshot", {"symbol": "SOLUSDT"}
            )
            return ticker, klines, snapshot

    ticker, klines, snapshot = asyncio.run(exercise())

    assert ticker.structured_content == {
        "symbol": "SOLUSDT",
        "last_price": "150.25",
        "high_24h": "155",
        "low_24h": "145",
        "close_time_ms": 2_000,
    }
    assert klines.structured_content["count"] == 2
    assert klines.structured_content["klines"][0]["volume"] == "12.5"
    assert snapshot.structured_content["intervals"]["5m"]["sar"]["direction"] == "long"
    combined = repr(
        (ticker.structured_content, klines.structured_content, snapshot.structured_content)
    )
    assert "must-not-leak" not in combined
    assert "credentials" not in combined
    assert "source" not in snapshot.structured_content


def test_invalid_limit_is_rejected_before_dependency_call():
    calls = []

    def read_klines(*args):
        calls.append(args)
        return []

    async def exercise():
        async with Client(_server(klines_reader=read_klines)) as client:
            return await client.call_tool(
                "get_completed_klines", {"symbol": "SOLUSDT", "interval": "5m", "limit": 201}
            )

    result = asyncio.run(exercise())
    assert result.is_error is True
    assert calls == []


def test_provider_failures_do_not_disclose_exception_or_secrets():
    def fail(_symbol):
        raise RuntimeError("api_key=top-secret provider.internal.local")

    async def exercise():
        async with Client(_server(ticker_reader=fail)) as client:
            return await client.call_tool("get_ticker", {"symbol": "SOLUSDT"})

    result = asyncio.run(exercise())
    rendered = repr(result.model_dump())
    assert result.is_error is True
    assert "top-secret" not in rendered
    assert "provider.internal.local" not in rendered


def test_uncompleted_or_oversized_market_data_is_rejected():
    incomplete = [_bar(0), replace(_bar(1), closed=False)]

    async def exercise():
        async with Client(
            _server(klines_reader=lambda _symbol, _interval, _limit: incomplete)
        ) as client:
            return await client.call_tool(
                "get_completed_klines", {"symbol": "SOLUSDT", "interval": "5m", "limit": 2}
            )

    result = asyncio.run(exercise())
    assert result.is_error is True


def test_malformed_snapshot_error_does_not_echo_untrusted_values():
    poisoned = _snapshot("SOLUSDT")
    poisoned["intervals"]["5m"]["sar"]["direction"] = "api_key=TOP_SECRET"

    async def exercise():
        async with Client(_server(snapshot_reader=lambda _symbol: poisoned)) as client:
            return await client.call_tool(
                "get_multi_timeframe_snapshot", {"symbol": "SOLUSDT"}
            )

    result = asyncio.run(exercise())
    rendered = repr(result.model_dump())
    assert result.is_error is True
    assert "TOP_SECRET" not in rendered
    assert "api_key" not in rendered


def test_snapshot_rejects_bar_after_causal_cutoff():
    future = _snapshot("SOLUSDT")
    future["intervals"]["5m"]["bar_closed_at"] = "2026-08-22T00:09:59.999Z"

    async def exercise():
        async with Client(_server(snapshot_reader=lambda _symbol: future)) as client:
            return await client.call_tool(
                "get_multi_timeframe_snapshot", {"symbol": "SOLUSDT"}
            )

    assert asyncio.run(exercise()).is_error is True
