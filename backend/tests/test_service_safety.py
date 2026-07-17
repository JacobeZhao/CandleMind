import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.app import datastore
from backend.app import data_layout
from backend.app.rl.data import attach_funding_cashflow, load_ml_scored_bars
from backend.app.routes import settings as settings_routes
from backend.app.routes import strategy as strategy_routes
from backend.app.routes.settings import SettingsIn
from backend.app.services.bot_engine import BotEngine
from backend.app.services.ml_strategy import load_scored_bars


class _ExchangeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def futures_exchange_info(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def test_settings_payload_is_a_true_partial_update():
    body = SettingsIn(testnet=False)
    assert body.model_dump(exclude_unset=True) == {"testnet": False}


def test_live_trading_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CANDLEMIND_ENABLE_LIVE_TRADING", raising=False)
    assert strategy_routes._live_trading_enabled() is False
    assert BotEngine().paper is True


def test_bot_engine_rejects_unauthorized_live_start():
    engine = BotEngine()
    with pytest.raises(ValueError, match="authorization"):
        asyncio.run(
            engine.start(
                object(),
                {
                    "symbol": "BTCUSDT",
                    "interval": "5m",
                    "strategy_type": "ml_trend",
                    "paper": False,
                },
            )
        )
    assert engine.running is False


def test_settings_roll_back_when_network_connection_fails(monkeypatch):
    current = SimpleNamespace(
        api_key_test_enc="test-key",
        api_secret_test_enc="test-secret",
        api_key_main_enc="main-key",
        api_secret_main_enc="main-secret",
        api_key_enc=None,
        api_secret_enc=None,
        testnet=True,
        symbol="BTCUSDT",
        interval="15m",
        proxy_url=None,
    )

    class _Query:
        def first(self):
            return current

    class _Db:
        committed = False
        rolled_back = False

        def query(self, _model):
            return _Query()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    async def _fail(_settings):
        raise OSError("offline")

    db = _Db()
    monkeypatch.setattr(settings_routes, "_connect_active", _fail)
    with pytest.raises(HTTPException):
        asyncio.run(settings_routes.save_settings(SettingsIn(testnet=False), db=db))
    assert not db.committed
    assert db.rolled_back


def test_exchange_filters_are_validated_and_cached():
    client = _ExchangeClient(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    ],
                }
            ]
        }
    )
    engine = BotEngine()
    assert engine._get_filters(client, "BTCUSDT") == (0.01, 0.1)
    assert engine._get_filters(client, "BTCUSDT") == (0.01, 0.1)
    assert client.calls == 1


@pytest.mark.parametrize(
    "client",
    [
        _ExchangeClient(error=OSError("offline")),
        _ExchangeClient({"symbols": []}),
        _ExchangeClient(
            {"symbols": [{"symbol": "BTCUSDT", "filters": []}]}
        ),
    ],
)
def test_exchange_filters_fail_closed(client):
    with pytest.raises(RuntimeError):
        BotEngine()._get_filters(client, "BTCUSDT")


def test_funding_feature_is_mapped_to_environment_cashflow():
    bars = pd.DataFrame({"5m_funding_rate": [0.0001, None, -0.0002]})
    result = attach_funding_cashflow(bars)
    assert result["funding_rate"].tolist() == [0.0001, 0.0, -0.0002]


def test_ml_scored_loader_maps_funding_cashflow(monkeypatch):
    source = pd.DataFrame(
        {"close": [100.0], "long_prob": [0.6], "short_prob": [0.4], "5m_funding_rate": [0.0001]}
    )
    monkeypatch.setattr(
        "backend.app.services.ml_strategy.load_scored_bars",
        lambda symbol, start=None, end=None: source.copy(),
    )
    result = load_ml_scored_bars("BTCUSDT")
    assert result.loc[0, "funding_rate"] == 0.0001


def test_multi_horizon_scoring_requires_explicit_variant():
    with pytest.raises(ValueError, match="multi_horizon_variant"):
        load_scored_bars("BTCUSDT", include_multi_horizon=True)


def test_non_windows_datastore_roots_do_not_include_drive_letters():
    assert datastore._configured_roots(None, platform="posix") == []
    assert datastore._configured_roots("/market", platform="posix") == [datastore.Path("/market")]


def test_non_windows_data_root_requires_explicit_external_path(tmp_path: Path):
    runtime_only = tmp_path / "runtime-only"
    with pytest.raises(data_layout.DataLayoutError, match="required outside Windows"):
        data_layout.select_data_root(
            market_data_dir=None,
            data_dir=str(runtime_only),
            platform="posix",
            default_windows_root=tmp_path / "unused",
        )
    assert not runtime_only.exists()


def _complete_data_root(root: Path) -> Path:
    for name in data_layout.REQUIRED_DIRECTORIES:
        (root / name).mkdir(parents=True)
    return root


def test_explicit_market_data_dir_fails_closed_without_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authoritative = _complete_data_root(tmp_path / "authoritative")
    fallback = tmp_path / "fallback"

    def reject_writes(_path: Path) -> None:
        raise data_layout.DataLayoutError("data root is not writable")

    monkeypatch.setattr(data_layout, "assert_writable_directory", reject_writes)
    with pytest.raises(data_layout.DataLayoutError, match="not writable"):
        data_layout.select_data_root(
            market_data_dir=str(authoritative),
            data_dir=str(fallback),
            platform="nt",
            default_windows_root=tmp_path / "unused-default",
        )

    assert not fallback.exists()


@pytest.mark.parametrize("configured", ["", "missing-authoritative-root"])
def test_explicit_invalid_market_data_dir_never_falls_back(
    tmp_path: Path,
    configured: str,
):
    fallback = tmp_path / "fallback"
    market_data_dir = configured or ""
    if configured:
        market_data_dir = str(tmp_path / configured)

    with pytest.raises(data_layout.DataLayoutError):
        data_layout.select_data_root(
            market_data_dir=market_data_dir,
            data_dir=str(fallback),
            platform="nt",
            default_windows_root=_complete_data_root(tmp_path / "windows-default"),
        )

    assert not fallback.exists()


def test_windows_default_uses_complete_root_and_fails_closed(tmp_path: Path):
    authoritative = _complete_data_root(tmp_path / "authoritative")
    selection = data_layout.select_data_root(
        market_data_dir=None,
        data_dir=str(tmp_path / "fallback-unused"),
        platform="nt",
        default_windows_root=authoritative,
    )
    assert selection.root == authoritative.resolve()
    assert selection.authoritative is True

    fallback = tmp_path / "fallback"
    with pytest.raises(data_layout.DataLayoutError):
        data_layout.select_data_root(
            market_data_dir=None,
            data_dir=str(fallback),
            platform="nt",
            default_windows_root=tmp_path / "incomplete-default",
        )
    assert not fallback.exists()


def test_datastore_only_creates_runtime_subdirectories_for_authoritative_root(
    tmp_path: Path,
):
    root = _complete_data_root(tmp_path / "authoritative")
    before = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
    }
    environment = os.environ.copy()
    environment["MARKET_DATA_DIR"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", "from backend.app import datastore; print(datastore.MARKET_ROOT)"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    after = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
    }
    assert after - before == {"runtime/journal", "runtime/regime_cache"}
    assert (root / "runtime" / "journal").is_dir()
    assert (root / "runtime" / "regime_cache").is_dir()
    assert not (
        root / "models" / "current" / "ml_trend_lgbm_catboost_20260709"
    ).exists()
