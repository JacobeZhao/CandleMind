"""Narrow Binance market-data capability for analysis code."""

from __future__ import annotations

from typing import Any

from .binance_usdm_gateway import BinanceUsdMGateway


class ReadOnlyMarketGateway:
    """Expose only server time and public futures candlesticks."""

    __slots__ = ("_gateway",)

    def __init__(self, client: Any) -> None:
        self._gateway = (
            client if isinstance(client, BinanceUsdMGateway) else BinanceUsdMGateway(client)
        )

    def server_time(self) -> int:
        return self._gateway.server_time()

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
        return self._gateway.klines(**parameters)
