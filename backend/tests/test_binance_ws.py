import asyncio
import json
from types import SimpleNamespace

import aiohttp

from backend.app.binance_ws import BinanceWSClient
from backend.app import binance_ws


def _event(**values):
    return json.dumps({"stream": "test", "data": values})


def _ticker(symbol="SOLUSDT", event_time=100, price="145.25"):
    return _event(
        e="24hrTicker",
        E=event_time,
        s=symbol,
        c=price,
        P="3.5",
        h="150",
        l="138",
        q="1234567",
    )


def _mark(symbol="SOLUSDT", event_time=100, price="145.20"):
    return _event(
        e="markPriceUpdate",
        E=event_time,
        s=symbol,
        p=price,
        i="145.18",
        r="0.0001",
        T=1_725_000_000_000,
    )


def test_combined_stream_merges_ticker_and_mark_price(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)

    asyncio.run(client._handle(_mark()))
    assert broadcasts == []

    asyncio.run(client._handle(_ticker()))

    assert broadcasts == [{
        "type": "ticker",
        "data": {
            "symbol": "SOLUSDT",
            "price": "145.25",
            "change": "3.5",
            "high": "150",
            "low": "138",
            "volume": "1234567",
            "markPrice": "145.20",
            "indexPrice": "145.18",
            "fundingRate": "0.0001",
            "nextFundingTime": 1_725_000_000_000,
        },
    }]


def test_ticker_broadcasts_without_fabricating_mark_price(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker()))

    data = broadcasts[0]["data"]
    assert data["price"] == "145.25"
    assert "markPrice" not in data


def test_out_of_order_and_incomplete_events_do_not_replace_cache(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker(event_time=200, price="150")))
    asyncio.run(client._handle(_ticker(event_time=100, price="120")))
    asyncio.run(client._handle(_event(e="markPriceUpdate", E=300, s="SOLUSDT")))

    assert len(broadcasts) == 1
    assert broadcasts[0]["data"]["price"] == "150"
    assert "markPrice" not in broadcasts[0]["data"]


def test_out_of_order_mark_price_does_not_replace_newer_value(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker()))
    asyncio.run(client._handle(_mark(event_time=300, price="151")))
    asyncio.run(client._handle(_mark(event_time=200, price="121")))

    assert broadcasts[-1]["data"]["markPrice"] == "151"
    assert len(broadcasts) == 2


def test_symbol_switch_clears_cache_and_ignores_old_subscription(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    client._subscription_id = 5
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker(), "SOLUSDT", 5))
    client.symbol = "BTCUSDT"
    client._subscription_id = 6
    client._reset_stream_state()

    asyncio.run(client._handle(_mark(), "SOLUSDT", 5))
    asyncio.run(client._handle(_ticker(symbol="BTCUSDT"), "BTCUSDT", 6))

    assert len(broadcasts) == 2
    assert broadcasts[-1]["data"]["symbol"] == "BTCUSDT"
    assert "markPrice" not in broadcasts[-1]["data"]


def test_plain_events_remain_supported(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    plain = json.dumps(json.loads(_ticker())["data"])
    asyncio.run(client._handle(plain))

    assert broadcasts[0]["data"]["symbol"] == "SOLUSDT"


def test_connect_subscribes_to_both_raw_streams(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    client.testnet = False
    client._running = True
    connected_urls = []
    subscriptions = []
    broadcasts = []

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send_json(self, payload):
            subscriptions.append(payload)

        def __aiter__(self):
            self._messages = iter((
                SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=_ticker()),
            ))
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def ws_connect(self, url, **_kwargs):
            connected_urls.append(url)
            return FakeWebSocket()

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)

    asyncio.run(client._connect())

    assert connected_urls == ["wss://fstream.binance.com/ws"]
    assert subscriptions == [{
        "method": "SUBSCRIBE",
        "params": ["solusdt@ticker", "solusdt@markPrice@1s"],
        "id": 1,
    }]
    assert broadcasts[0]["data"]["symbol"] == "SOLUSDT"
