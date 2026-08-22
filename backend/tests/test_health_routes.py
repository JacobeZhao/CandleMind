import asyncio
from types import SimpleNamespace

from backend.app.routes import health as health_routes


class _Query:
    def __init__(self, settings):
        self.settings = settings

    def first(self):
        return self.settings


class _Db:
    def __init__(self, settings):
        self.settings = settings

    def query(self, _model):
        return _Query(self.settings)


def test_geolocation_is_advisory_not_binance_restriction_proof(monkeypatch):
    response = SimpleNamespace(
        json=lambda: {
            "query": "203.0.113.10",
            "countryCode": "US",
            "country": "United States",
        }
    )
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)

    result = asyncio.run(
        health_routes.health(db=_Db(SimpleNamespace(
            proxy_url=None,
            testnet=True,
            exchange_provider="binance",
        )))
    )

    assert result["exit_ip"] == "203.0.113.10"
    assert result["country"] == "United States"
    assert result["restricted"] is None
    assert result["provider"] == "binance"


def test_health_reports_selected_unavailable_provider_as_disconnected(monkeypatch):
    monkeypatch.setattr(health_routes.app_state, "client", object())
    monkeypatch.setattr(health_routes.app_state, "exchange_provider", "okx")
    monkeypatch.setattr(
        "requests.get",
        lambda *_args, **_kwargs: SimpleNamespace(json=lambda: {}),
    )

    result = asyncio.run(health_routes.health(db=_Db(SimpleNamespace(
        proxy_url=None,
        testnet=True,
        exchange_provider="okx",
    ))))

    assert result["provider"] == "okx"
    assert result["connected"] is False
