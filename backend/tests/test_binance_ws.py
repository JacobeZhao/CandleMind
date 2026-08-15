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


def _trade(symbol="SOLUSDT", event_time=100, price="145.30"):
    return _event(
        e="aggTrade",
        E=event_time,
        s=symbol,
        a=123,
        p=price,
        q="2.5",
    )


async def _publish(client):
    client._running = True
    client._subscription_id = 1
    await client._publish_latest(1)


def test_combined_stream_merges_trade_ticker_and_mark_price(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)

    asyncio.run(client._handle(_mark()))
    assert broadcasts == []

    asyncio.run(client._handle(_ticker(event_time=110)))
    asyncio.run(client._handle(_trade(event_time=120)))
    asyncio.run(_publish(client))

    assert broadcasts == [{
        "type": "ticker",
        "data": {
            "symbol": "SOLUSDT",
            "price": "145.30",
            "change": "3.5",
            "high": "150",
            "low": "138",
            "volume": "1234567",
            "markPrice": "145.20",
            "indexPrice": "145.18",
            "fundingRate": "0.0001",
            "nextFundingTime": 1_725_000_000_000,
            "eventTime": 120,
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
    asyncio.run(_publish(client))

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
    asyncio.run(_publish(client))

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
    asyncio.run(_publish(client))

    assert broadcasts[-1]["data"]["markPrice"] == "151"
    assert len(broadcasts) == 1


def test_symbol_switch_clears_cache_and_ignores_old_subscription(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    client._subscription_id = 5
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker(), "SOLUSDT", 5))
    client._running = True
    asyncio.run(client._publish_latest(5))
    client.symbol = "BTCUSDT"
    client._subscription_id = 6
    client._reset_stream_state()

    asyncio.run(client._handle(_mark(), "SOLUSDT", 5))
    asyncio.run(client._handle(_ticker(symbol="BTCUSDT"), "BTCUSDT", 6))
    asyncio.run(client._publish_latest(6))

    assert len(broadcasts) == 2
    assert broadcasts[-1]["data"]["symbol"] == "BTCUSDT"
    assert "markPrice" not in broadcasts[-1]["data"]


def test_running_symbol_switch_cancels_tasks_and_clears_cache(monkeypatch):
    client = BinanceWSClient(broadcast_interval=60)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_run", idle)

    async def scenario():
        await client.start("SOLUSDT", False)
        old_subscription = client._subscription_id
        old_run_task = client._task
        old_publisher_task = client._publisher_task
        await client._handle(_ticker(), "SOLUSDT", old_subscription)
        assert client._stream_cache

        await client.switch_symbol("BTCUSDT")
        assert old_run_task.cancelled()
        assert old_publisher_task.cancelled()
        assert client.symbol == "BTCUSDT"
        assert client._stream_cache == {}
        assert client._stream_event_times == {}
        assert client._subscription_id != old_subscription

        await client._handle(_trade(), "SOLUSDT", old_subscription)
        assert client._stream_cache == {}
        await client.stop()
        assert client._task is None
        assert client._publisher_task is None
        assert not client._running

    asyncio.run(scenario())


def test_plain_events_remain_supported(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    plain = json.dumps(json.loads(_ticker())["data"])
    asyncio.run(client._handle(plain))
    asyncio.run(_publish(client))

    assert broadcasts[0]["data"]["symbol"] == "SOLUSDT"


def test_connect_subscribes_to_all_raw_streams(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    client.testnet = False
    client._running = True
    connected_urls = []
    subscriptions = []

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

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    asyncio.run(client._connect())

    assert connected_urls == ["wss://fstream.binance.com/ws"]
    assert subscriptions == [{
        "method": "SUBSCRIBE",
        "params": [
            "solusdt@aggTrade",
            "solusdt@ticker",
            "solusdt@markPrice@1s",
        ],
        "id": 1,
    }]


def test_publish_loop_coalesces_events_to_latest_every_500ms(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)

    async def scenario():
        client._running = True
        client._subscription_id = 1
        task = asyncio.create_task(client._publish_loop(1))
        try:
            await client._handle(_ticker(event_time=100, price="140"))
            await client._handle(_trade(event_time=101, price="141"))
            await client._handle(_trade(event_time=102, price="142"))
            await asyncio.sleep(0.25)
            assert broadcasts == []
            await asyncio.sleep(0.30)
            assert len(broadcasts) == 1
            assert broadcasts[0]["data"]["price"] == "142"
            assert broadcasts[0]["data"]["eventTime"] == 102
            await asyncio.sleep(0.55)
            assert len(broadcasts) == 1
        finally:
            client._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
    assert client.broadcast_interval == 0.5


def test_slow_broadcast_keeps_only_one_pending_latest_snapshot(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def capture(payload):
        broadcasts.append(payload)
        entered.set()
        await release.wait()

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)

    async def scenario():
        client._running = True
        client._subscription_id = 1
        await client._handle(_ticker(event_time=100, price="140"))
        publishing = asyncio.create_task(client._publish_latest(1))
        await entered.wait()
        await client._handle(_trade(event_time=101, price="141"))
        await client._handle(_trade(event_time=102, price="142"))
        assert len(broadcasts) == 1
        release.set()
        await publishing
        await client._publish_latest(1)

    asyncio.run(scenario())
    assert [item["data"]["price"] for item in broadcasts] == ["140", "142"]


def test_out_of_order_trade_does_not_replace_newer_price(monkeypatch):
    client = BinanceWSClient()
    client.symbol = "SOLUSDT"
    broadcasts = []

    async def capture(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(binance_ws.manager, "broadcast", capture)
    asyncio.run(client._handle(_ticker(event_time=100, price="140")))
    asyncio.run(client._handle(_trade(event_time=300, price="151")))
    asyncio.run(client._handle(_trade(event_time=200, price="121")))
    asyncio.run(_publish(client))

    assert broadcasts[0]["data"]["price"] == "151"
    assert broadcasts[0]["data"]["eventTime"] == 300
