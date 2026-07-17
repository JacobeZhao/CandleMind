from pathlib import Path

import pytest

from backend.app import datastore


def _candidate_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "models" / "candidates" / "supervised"
    monkeypatch.setattr(datastore, "SUPERVISED_CANDIDATES_DIR", root)
    return root


def _current_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "models" / "current"
    root.mkdir(parents=True)
    monkeypatch.setattr(datastore, "MODELS_CURRENT_DIR", root)
    monkeypatch.setattr(datastore, "ACTIVE_MODEL_RELEASE_FILE", root / "ACTIVE")
    return root


def test_supervised_candidate_dir_creates_versioned_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _candidate_root(monkeypatch, tmp_path)

    candidate = datastore.supervised_candidate_dir(
        "trend_20260717_01", create=True
    )

    assert candidate == (root / "trend_20260717_01").resolve()
    assert candidate.is_dir()


@pytest.mark.parametrize("release_id", ["", "../current", "bad/name", " bad"])
def test_supervised_candidate_dir_rejects_unsafe_release_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release_id: str
) -> None:
    _candidate_root(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="release_id"):
        datastore.supervised_candidate_dir(release_id)


def test_supervised_output_rejects_current_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _candidate_root(monkeypatch, tmp_path)
    current = tmp_path / "models" / "current" / "production"

    with pytest.raises(ValueError, match="models.*candidates.*supervised"):
        datastore.validate_supervised_candidate_dir(current, create=True)

    assert not current.exists()


def test_current_release_uses_single_directory_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = _current_root(monkeypatch, tmp_path)
    release = current / "release_20260709"
    release.mkdir()

    assert datastore.resolve_current_model_release() == release.resolve()


def test_active_pointer_selects_one_of_multiple_releases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = _current_root(monkeypatch, tmp_path)
    selected = current / "release_b"
    (current / "release_a").mkdir()
    selected.mkdir()
    (current / "ACTIVE").write_text("release_b\n", encoding="utf-8")

    assert datastore.resolve_current_model_release() == selected.resolve()


def test_multiple_current_releases_require_active_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = _current_root(monkeypatch, tmp_path)
    (current / "release_a").mkdir()
    (current / "release_b").mkdir()

    with pytest.raises(RuntimeError, match="ACTIVE"):
        datastore.resolve_current_model_release()


def test_active_pointer_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = _current_root(monkeypatch, tmp_path)
    (current / "ACTIVE").write_text("../archive/release_a", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid active"):
        datastore.resolve_current_model_release()
