import asyncio

from backend.app.routes import market


class FakeBinanceClient:
    def futures_symbol_ticker(self, *, symbol):
        return {"symbol": symbol, "price": "145.25"}

    def futures_ticker(self, *, symbol):
        return {
            "symbol": symbol,
            "priceChangePercent": "3.50",
            "highPrice": "150.00",
            "lowPrice": "138.00",
            "quoteVolume": "1234567.89",
        }

    def futures_mark_price(self, *, symbol):
        return {
            "symbol": symbol,
            "markPrice": "145.20",
            "indexPrice": "145.18",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1_725_000_000_000,
        }


def test_ticker_merges_latest_stats_and_real_mark_price(monkeypatch):
    client = FakeBinanceClient()
    monkeypatch.setattr(market.app_state, "client", client)

    result = asyncio.run(market.ticker("solusdt"))

    assert result == {
        "symbol": "SOLUSDT",
        "price": "145.25",
        "priceChangePercent": "3.50",
        "highPrice": "150.00",
        "lowPrice": "138.00",
        "quoteVolume": "1234567.89",
        "markPrice": "145.20",
        "indexPrice": "145.18",
        "lastFundingRate": "0.0001",
        "nextFundingTime": 1_725_000_000_000,
    }


def test_ticker_starts_all_three_calls_before_waiting(monkeypatch):
    client = FakeBinanceClient()
    monkeypatch.setattr(market.app_state, "client", client)
    started = []
    release = asyncio.Event()

    async def controlled_to_thread(function, **kwargs):
        started.append(function.__name__)
        if len(started) == 3:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        return function(**kwargs)

    monkeypatch.setattr(market.asyncio, "to_thread", controlled_to_thread)

    result = asyncio.run(market.ticker("SOLUSDT"))

    assert set(started) == {
        "futures_symbol_ticker",
        "futures_ticker",
        "futures_mark_price",
    }
    assert result["markPrice"] == "145.20"


def test_ticker_does_not_fabricate_optional_mark_fields(monkeypatch):
    client = FakeBinanceClient()
    client.futures_mark_price = lambda **_kwargs: {"markPrice": "145.20"}
    monkeypatch.setattr(market.app_state, "client", client)

    result = asyncio.run(market.ticker("SOLUSDT"))

    assert result["markPrice"] == "145.20"
    assert "indexPrice" not in result
    assert "lastFundingRate" not in result
    assert "nextFundingTime" not in result
