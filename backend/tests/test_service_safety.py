import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app import datastore
from backend.app import data_layout
from backend.app.routes import settings as settings_routes
from backend.app.routes.settings import SettingsIn


def test_settings_payload_is_a_true_partial_update():
    body = SettingsIn(testnet=False)
    assert body.model_dump(exclude_unset=True) == {"testnet": False}


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


def test_required_layout_matches_current_data_producers_and_consumers():
    required = set(data_layout.REQUIRED_DIRECTORIES)

    assert "raw/klines_archive" in required
    assert "raw/klines_json" not in required
    assert "normalized/ohlcv_parquet" in required
    assert "normalized/ema/releases" in required
    assert "normalized/derivatives/releases" in required


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


def test_data_root_selection_requires_writable_root_by_default(tmp_path: Path):
    authoritative = _complete_data_root(tmp_path / "authoritative")
    writable_requirements: list[bool] = []

    def record_validation(path: Path, *, require_writable: bool) -> Path:
        writable_requirements.append(require_writable)
        return path.resolve()

    selection = data_layout.select_data_root(
        market_data_dir=str(authoritative),
        data_dir=None,
        validator=record_validation,
    )

    assert selection.root == authoritative.resolve()
    assert writable_requirements == [True]


def test_datastore_selects_market_data_root_without_write_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authoritative = _complete_data_root(tmp_path / "authoritative")
    received: dict[str, object] = {}

    def record_selection(**kwargs) -> data_layout.DataRootSelection:
        received.update(kwargs)
        return data_layout.DataRootSelection(
            root=authoritative.resolve(), authoritative=True
        )

    monkeypatch.setattr(datastore, "select_data_root", record_selection)
    selection = datastore._resolve_root(configured=str(authoritative))

    assert selection.root == authoritative.resolve()
    assert received["require_writable"] is False


def test_read_only_selection_still_rejects_layout_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authoritative = _complete_data_root(tmp_path / "authoritative")
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    escaped_child = authoritative.resolve() / "raw" / "funding"
    escaped_target = escaped.resolve()
    original_resolve = Path.resolve

    def resolve_with_escape(path: Path, strict: bool = False) -> Path:
        if path == escaped_child:
            return escaped_target
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_escape)

    with pytest.raises(data_layout.DataLayoutError, match="escapes the root"):
        data_layout.select_data_root(
            market_data_dir=str(authoritative),
            data_dir=None,
            require_writable=False,
        )


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


def test_datastore_import_does_not_create_directories(
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
    assert after == before
    assert not (
        root / "models" / "current" / "ml_trend_lgbm_catboost_20260709"
    ).exists()
