"""Explicit provider registry with no implicit fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import (
    AccountPort,
    ExchangeBinding,
    ExchangeCapabilities,
    MarketDataPort,
    TradingPort,
    normalize_provider,
)


class ProviderUnavailableError(LookupError):
    def __init__(self, provider: str) -> None:
        self.provider = normalize_provider(provider)
        super().__init__(f"exchange provider '{self.provider}' is unavailable")


@dataclass(frozen=True, slots=True)
class ExchangeAdapter:
    binding: ExchangeBinding
    capabilities: ExchangeCapabilities
    market: MarketDataPort
    account: AccountPort | None = None
    trading: TradingPort | None = None

    def __post_init__(self) -> None:
        if self.market.binding != self.binding:
            raise ValueError("market-data port has a mismatched binding")
        if self.account is not None and self.account.binding != self.binding:
            raise ValueError("account port has a mismatched binding")
        if self.trading is not None and self.trading.binding != self.binding:
            raise ValueError("trading port has a mismatched binding")
        if not self.capabilities.market_data:
            raise ValueError("an exchange adapter must expose market data")
        if self.capabilities.account != (self.account is not None):
            raise ValueError("account capability does not match its port")
        if self.capabilities.trading != (self.trading is not None):
            raise ValueError("trading capability does not match its port")


AdapterFactory = Callable[[ExchangeBinding], ExchangeAdapter]


class ExchangeProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, provider: str, factory: AdapterFactory) -> None:
        normalized = normalize_provider(provider)
        if normalized in self._factories:
            raise ValueError(f"exchange provider '{normalized}' is already registered")
        self._factories[normalized] = factory

    def resolve(self, binding: ExchangeBinding) -> ExchangeAdapter:
        factory = self._factories.get(binding.provider)
        if factory is None:
            raise ProviderUnavailableError(binding.provider)
        adapter = factory(binding)
        if adapter.binding != binding:
            raise ValueError("exchange adapter returned a mismatched binding")
        return adapter

    def available_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
