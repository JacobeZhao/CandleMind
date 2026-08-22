import asyncio

import pytest

from backend.app.routes import market


@pytest.fixture(autouse=True)
def binance_provider(monkeypatch):
    monkeypatch.setattr(market.app_state, "exchange_provider", "binance")


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
        "symbol_ticker",
        "ticker",
        "mark_price",
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


def test_ticker_returns_structured_retryable_gateway_failure(monkeypatch):
    client = FakeBinanceClient()
    client.futures_symbol_ticker = lambda **_kwargs: (_ for _ in ()).throw(
        TimeoutError("credential-like private detail")
    )
    monkeypatch.setattr(market.app_state, "client", client)

    try:
        asyncio.run(market.ticker("SOLUSDT"))
    except market.HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail == {
            "code": "binance_unavailable",
            "message": "Binance 请求超时，服务器已完成自动重试。",
            "retryable": True,
        }
        assert "private" not in str(exc.detail)
    else:
        raise AssertionError("expected structured gateway failure")


@pytest.mark.parametrize(
    ("endpoint", "args"),
    [
        (market.ticker, ("SOLUSDT",)),
        (market.klines, ("SOLUSDT",)),
        (market.symbols, ()),
    ],
)
def test_non_binance_market_routes_never_construct_gateway(monkeypatch, endpoint, args):
    class FailGateway:
        def __init__(self, _client):
            raise AssertionError("Binance gateway must not be constructed")

    monkeypatch.setattr(market.app_state, "client", object())
    monkeypatch.setattr(market.app_state, "exchange_provider", "bybit")
    monkeypatch.setattr(market, "BinanceUsdMGateway", FailGateway)

    with pytest.raises(market.HTTPException) as raised:
        asyncio.run(endpoint(*args))

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "exchange_provider_unavailable",
        "message": "所选市场暂未接入，敬请期待。",
        "retryable": False,
        "provider": "bybit",
    }
