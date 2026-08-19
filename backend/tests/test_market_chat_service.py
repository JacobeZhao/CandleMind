import asyncio

import pytest

from backend.app.services import market_chat


def _klines(count=80, interval_ms=3_600_000, start=1_700_000_000_000):
    rows = []
    for index in range(count):
        open_time = start + index * interval_ms
        price = 100.0 + index * 0.4
        rows.append(
            [
                open_time,
                str(price),
                str(price + 1.0),
                str(price - 0.8),
                str(price + 0.3),
                str(1000 + index),
                open_time + interval_ms - 1,
                "0",
                10,
                "0",
                "0",
                "0",
            ]
        )
    return rows


def test_snapshot_drops_unfinished_bars_and_computes_indicators():
    rows = _klines()
    server_time = rows[-2][6] + 1
    snapshot = market_chat.build_market_snapshot(
        symbol="SOLUSDT",
        interval="1h",
        server_time_ms=server_time,
        current_raw=rows,
        hourly_raw=rows,
    )

    assert snapshot["current_bar_closed_at"] == market_chat._iso_utc(rows[-2][6])
    assert snapshot["adx_bar_closed_at"] == market_chat._iso_utc(rows[-2][6])
    assert snapshot["sar"]["direction"] in {"long", "short"}
    assert snapshot["adx_1h"]["adx"] is not None
    assert snapshot["adx_1h"]["plus_di"] is not None
    assert snapshot["adx_1h"]["minus_di"] is not None
    assert len(snapshot["recent_normalized_bars"]) == 24


def test_large_candle_uses_previous_bar_atr_without_current_bar_leakage():
    rows = _klines()
    rows[-1][1] = "100.0"
    rows[-1][2] = "106.0"
    rows[-1][3] = "99.5"
    rows[-1][4] = "105.0"
    server_time = rows[-1][6] + 1

    snapshot = market_chat.build_market_snapshot(
        symbol="SOLUSDT",
        interval="1h",
        server_time_ms=server_time,
        current_raw=rows,
        hourly_raw=rows,
    )
    frame = market_chat._closed_frame(rows, server_time)
    expected_ratio = 5.0 / market_chat._wilder_atr(frame).shift(1).iloc[-1]

    assert snapshot["candle"]["body_atr_ratio"] == round(expected_ratio, 6)
    assert snapshot["candle"]["large_body"] is True


def test_snapshot_rejects_insufficient_completed_bars():
    rows = _klines(count=30)
    with pytest.raises(market_chat.MarketDataError, match="completed"):
        market_chat.build_market_snapshot(
            symbol="SOLUSDT",
            interval="1h",
            server_time_ms=rows[10][6] + 1,
            current_raw=rows,
            hourly_raw=rows,
        )


def test_provider_receives_server_snapshot_and_no_client_market_facts(monkeypatch):
    rows = _klines()

    class Client:
        def futures_time(self):
            return {"serverTime": rows[-1][6] + 1}

        def futures_klines(self, **kwargs):
            return rows

    observed = {}

    async def fake_complete(provider, api_key, base_url, model, messages, proxy_url):
        observed.update(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            proxy_url=proxy_url,
        )
        return "analysis"

    monkeypatch.setattr(market_chat, "chat_complete", fake_complete)
    result = asyncio.run(
        market_chat.analyze_market(
            client=Client(),
            symbol="SOLUSDT",
            interval="1h",
            messages=[{"role": "user", "content": "当前市场周期？"}],
            provider_config={
                "provider": "openai",
                "api_key": "secret",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-test",
            },
            proxy_url=None,
        )
    )

    assert result.answer == "analysis"
    assert observed["messages"][0]["role"] == "system"
    assert "TRUSTED_MARKET_SNAPSHOT" in observed["messages"][0]["content"]
    assert observed["messages"][-1] == {"role": "user", "content": "当前市场周期？"}
    assert len(observed["messages"][0]["content"].encode()) < market_chat.MAX_CONTEXT_BYTES + 2000


def test_fetch_uses_separate_completed_hourly_data():
    current = _klines(interval_ms=300_000)
    hourly = _klines()
    calls = []

    class Client:
        def futures_time(self):
            return {"serverTime": hourly[-1][6] + 1}

        def futures_klines(self, **kwargs):
            calls.append(kwargs)
            return hourly if kwargs["interval"] == "1h" else current

    snapshot = asyncio.run(market_chat.fetch_market_snapshot(Client(), "SOLUSDT", "5m"))
    assert [call["interval"] for call in calls] == ["5m", "1h"]
    assert snapshot["interval"] == "5m"
