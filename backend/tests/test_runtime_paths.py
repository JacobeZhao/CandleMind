import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def _import_fresh(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_explicit_data_dir_always_takes_priority(tmp_path):
    runtime_paths = _import_fresh("backend.app.runtime_paths")
    explicit_path = tmp_path / "explicit-runtime"

    for platform in ("win32", "linux"):
        resolved = runtime_paths.resolve_runtime_data_dir(
            environ={"DATA_DIR": str(explicit_path)},
            platform=platform,
            windows_default_path=tmp_path / "windows-default",
            default_path=tmp_path / "other-default",
        )
        assert resolved == explicit_path


def test_windows_uses_injected_runtime_default_without_creating_it(tmp_path):
    runtime_paths = _import_fresh("backend.app.runtime_paths")
    windows_default = tmp_path / "windows-runtime"

    resolved = runtime_paths.resolve_runtime_data_dir(
        environ={},
        platform="win32",
        windows_default_path=windows_default,
        default_path=tmp_path / "other-default",
    )

    assert resolved == windows_default
    assert not windows_default.exists()
    assert runtime_paths.resolve_runtime_data_dir(
        environ={}, platform="win32"
    ) == Path("G:/CandleMind/CandleMind_data/runtime/app")


def test_non_windows_uses_injected_local_default(tmp_path):
    runtime_paths = _import_fresh("backend.app.runtime_paths")
    local_default = tmp_path / "local-data"

    resolved = runtime_paths.resolve_runtime_data_dir(
        environ={},
        platform="linux",
        windows_default_path=tmp_path / "windows-default",
        default_path=local_default,
    )

    assert resolved == local_default
    with pytest.raises(ValueError, match="DATA_DIR is required"):
        runtime_paths.resolve_runtime_data_dir(environ={}, platform="linux")


def test_database_and_fernet_key_share_explicit_runtime_directory(
    monkeypatch, tmp_path
):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("DATA_DIR", str(runtime_dir))

    runtime_paths = _import_fresh("backend.app.runtime_paths")
    database = _import_fresh("backend.app.database")
    security = _import_fresh("backend.app.security")

    try:
        security._get_fernet()

        database_path = Path(database.engine.url.database)
        key_path = runtime_dir / "secret.key"
        assert runtime_paths.RUNTIME_DATA_DIR == runtime_dir
        assert database_path.parent == key_path.parent == runtime_dir
        assert key_path.is_file()
    finally:
        database.engine.dispose()


def test_secret_key_creation_is_race_safe(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(runtime_dir))
    security = _import_fresh("backend.app.security")
    key_path = runtime_dir / "secret.key"

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _index: security._load_or_create_key(key_path), range(32)))

    assert len(set(keys)) == 1
    assert key_path.read_bytes() == keys[0]
    assert not list(runtime_dir.glob(".secret.key.*.tmp"))
    Fernet(keys[0])
