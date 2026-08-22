"""Stable exchange-provider identifiers shared by persistence and runtime code."""

from typing import Literal, TypeAlias, cast


BINANCE_PROVIDER = "binance"
EXCHANGE_PROVIDERS = ("binance", "okx", "bybit", "gateio", "a_share")
ExchangeProvider: TypeAlias = Literal[
    "binance", "okx", "bybit", "gateio", "a_share"
]


def normalize_exchange_provider(value: str | None) -> ExchangeProvider:
    provider = (value or BINANCE_PROVIDER).strip().lower()
    if provider not in EXCHANGE_PROVIDERS:
        raise ValueError(f"unsupported exchange provider: {provider}")
    return cast(ExchangeProvider, provider)


def is_binance_provider(value: str | None) -> bool:
    return normalize_exchange_provider(value) == BINANCE_PROVIDER


def unavailable_provider_detail(value: str | None) -> dict[str, object]:
    provider = normalize_exchange_provider(value)
    return {
        "code": "exchange_provider_unavailable",
        "message": "所选市场暂未接入，敬请期待。",
        "retryable": False,
        "provider": provider,
    }
