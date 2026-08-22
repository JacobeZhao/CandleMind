import pytest

from backend.app.exchanges.contracts import (
    ExchangeBinding,
    ExchangeCapabilities,
    ExchangeNetwork,
)
from backend.app.exchanges.registry import (
    ExchangeAdapter,
    ExchangeProviderRegistry,
    ProviderUnavailableError,
)


class MarketStub:
    def __init__(self, binding):
        self.binding = binding


class AccountStub:
    def __init__(self, binding):
        self.binding = binding


def _adapter(binding):
    return ExchangeAdapter(
        binding=binding,
        capabilities=ExchangeCapabilities(market_data=True),
        market=MarketStub(binding),
    )


def test_registry_resolves_only_explicitly_registered_provider():
    registry = ExchangeProviderRegistry()
    registry.register("binance", _adapter)
    binding = ExchangeBinding("BINANCE", ExchangeNetwork.TESTNET, "solusdt")

    adapter = registry.resolve(binding)

    assert adapter.binding == binding
    assert registry.available_providers() == ("binance",)


@pytest.mark.parametrize("provider", ["okx", "bybit", "gateio", "a_share"])
def test_unavailable_provider_never_falls_back_to_binance(provider):
    registry = ExchangeProviderRegistry()
    calls = []
    registry.register("binance", lambda binding: calls.append(binding) or _adapter(binding))

    with pytest.raises(ProviderUnavailableError) as caught:
        registry.resolve(ExchangeBinding(provider, "testnet", "SOLUSDT"))

    assert caught.value.provider == provider
    assert calls == []


def test_registry_rejects_adapter_with_different_scope():
    registry = ExchangeProviderRegistry()
    other = ExchangeBinding("binance", "mainnet", "BTCUSDT")
    registry.register("binance", lambda _binding: _adapter(other))

    with pytest.raises(ValueError, match="mismatched binding"):
        registry.resolve(ExchangeBinding("binance", "testnet", "SOLUSDT"))


def test_registry_rejects_duplicate_provider_registration():
    registry = ExchangeProviderRegistry()
    registry.register("binance", _adapter)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("BINANCE", _adapter)


def test_adapter_rejects_port_with_different_scope():
    binding = ExchangeBinding("binance", "testnet", "SOLUSDT")
    other = ExchangeBinding("binance", "testnet", "BTCUSDT")

    with pytest.raises(ValueError, match="market-data port"):
        ExchangeAdapter(
            binding=binding,
            capabilities=ExchangeCapabilities(market_data=True),
            market=MarketStub(other),
        )


@pytest.mark.parametrize(
    ("capabilities", "account", "message"),
    [
        (ExchangeCapabilities(account=True), None, "account capability"),
        (ExchangeCapabilities(account=False), "present", "account capability"),
    ],
)
def test_adapter_rejects_capability_port_mismatch(capabilities, account, message):
    binding = ExchangeBinding("binance", "testnet", "SOLUSDT")
    account_port = AccountStub(binding) if account else None

    with pytest.raises(ValueError, match=message):
        ExchangeAdapter(
            binding=binding,
            capabilities=capabilities,
            market=MarketStub(binding),
            account=account_port,
        )
