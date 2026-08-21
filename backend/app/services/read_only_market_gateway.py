"""Narrow Binance market-data capability for analysis code."""

from __future__ import annotations

from typing import Any


class ReadOnlyMarketGateway:
    """Expose only server time and public futures candlesticks."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def server_time(self) -> int:
        return int(self._client.futures_time()["serverTime"])

    def klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        end_time: int | None = None,
    ) -> Any:
        parameters: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if end_time is not None:
            parameters["endTime"] = end_time
        return self._client.futures_klines(**parameters)
