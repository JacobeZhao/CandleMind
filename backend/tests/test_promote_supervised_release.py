from pathlib import Path

import pytest

from backend.app import datastore
from backend.scripts.artifacts.create_model_release_manifest import (
    build_release_manifest,
    write_manifest_atomic,
)
from backend.scripts.artifacts.promote_supervised_release import promote_release


def _layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    models = tmp_path / "models"
    candidates = models / "candidates" / "supervised"
    releases = models / "releases"
    current = models / "current"
    current.mkdir(parents=True)
    monkeypatch.setattr(datastore, "MODELS_ROOT", models)
    monkeypatch.setattr(datastore, "SUPERVISED_CANDIDATES_DIR", candidates)
    monkeypatch.setattr(datastore, "MODELS_RELEASES_DIR", releases)
    monkeypatch.setattr(datastore, "MODELS_CURRENT_DIR", current)
    monkeypatch.setattr(datastore, "ACTIVE_MODEL_RELEASE_FILE", current / "ACTIVE")
    return candidates


def _sealed_candidate(root: Path, release_id: str = "release-a") -> Path:
    candidate = root / release_id
    candidate.mkdir(parents=True)
    (candidate / "model.pkl").write_bytes(b"model")
    (candidate / "thresholds.json").write_text("{}", encoding="utf-8")
    output = candidate / "release_manifest.json"
    payload = build_release_manifest(
        candidate,
        output=output,
        data_inventory_sha256="a" * 64,
        training_code_revision="abc123",
        training_data_snapshot="snapshot-1",
        cost_assumptions="taker=10bp;slippage=2bp",
    )
    write_manifest_atomic(output, payload)
    return candidate


def test_promotion_moves_complete_release_and_activates_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidates = _layout(monkeypatch, tmp_path)
    candidate = _sealed_candidate(candidates)

    release = promote_release("release-a")

    assert not candidate.exists()
    assert (release / "model.pkl").read_bytes() == b"model"
    assert datastore.ACTIVE_MODEL_RELEASE_FILE.read_text(encoding="utf-8") == "release-a\n"
    assert datastore.resolve_current_model_release() == release.resolve()


def test_promotion_rejects_tampered_artifact_without_moving_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidates = _layout(monkeypatch, tmp_path)
    candidate = _sealed_candidate(candidates)
    (candidate / "model.pkl").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="mismatch"):
        promote_release("release-a")

    assert candidate.is_dir()
    assert not datastore.ACTIVE_MODEL_RELEASE_FILE.exists()


def test_promotion_rejects_incomplete_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidates = _layout(monkeypatch, tmp_path)
    candidate = candidates / "release-a"
    candidate.mkdir(parents=True)
    (candidate / "model.pkl").write_bytes(b"model")
    output = candidate / "release_manifest.json"
    payload = build_release_manifest(
        candidate,
        output=output,
        data_inventory_sha256="b" * 64,
    )
    write_manifest_atomic(output, payload)

    with pytest.raises(ValueError, match="lineage"):
        promote_release("release-a")


def test_promotion_rejects_extra_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidates = _layout(monkeypatch, tmp_path)
    candidate = _sealed_candidate(candidates)
    (candidate / "nested").mkdir()

    with pytest.raises(ValueError, match="unsafe entries"):
        promote_release("release-a")

    assert candidate.is_dir()


def test_promotion_refuses_to_replace_existing_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidates = _layout(monkeypatch, tmp_path)
    candidate = _sealed_candidate(candidates)
    existing = datastore.MODELS_RELEASES_DIR / "release-a"
    existing.mkdir(parents=True)
    (existing / "sentinel").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        promote_release("release-a")

    assert candidate.is_dir()
    assert (existing / "sentinel").read_text(encoding="utf-8") == "keep"
    assert not datastore.ACTIVE_MODEL_RELEASE_FILE.exists()
