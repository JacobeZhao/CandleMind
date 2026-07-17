from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path

import pytest

from backend.scripts.artifacts import publish_deployment_cache
from backend.app.data_layout import REQUIRED_DIRECTORIES
from backend.scripts.artifacts import gdisk_cleanup


@pytest.fixture(autouse=True)
def isolated_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(publish_deployment_cache, "REPOSITORY_ROOT", repository)


@pytest.fixture
def cleanup_root(tmp_path: Path) -> Path:
    root = tmp_path / "CandleMind_data"
    for name in REQUIRED_DIRECTORIES:
        (root / name).mkdir(parents=True)
    (root / "models" / "rl" / "candidates").mkdir(parents=True)
    (root / "models" / "archive").mkdir(parents=True, exist_ok=True)
    (root / "experiments" / "reports" / "rl").mkdir(parents=True)
    return root


def test_sync_module_import_does_not_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_copy(*args, **kwargs):
        raise AssertionError("copy executed while importing module")

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    importlib.reload(publish_deployment_cache)


def test_sync_requires_explicit_destination() -> None:
    with pytest.raises(SystemExit):
        publish_deployment_cache.build_parser().parse_args([])


def _model_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "model.pkl").write_bytes(b"new model")
    (source / "thresholds.json").write_text('{"threshold": 0.5}', encoding="utf-8")
    (source / "notes.txt").write_text("ignore", encoding="utf-8")
    return source


def test_sync_models_default_staging_swap_publishes_exact_set(tmp_path: Path) -> None:
    source = _model_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "extra.pkl").write_bytes(b"stale")
    (destination / "nested").mkdir()

    copied = publish_deployment_cache.sync_models(source, destination)

    assert [path.name for path in copied] == ["model.pkl", "thresholds.json"]
    assert sorted(path.name for path in destination.iterdir()) == [
        "model.pkl",
        "thresholds.json",
    ]
    assert (destination / "model.pkl").read_bytes() == b"new model"
    assert not list(tmp_path.glob(".destination.*"))


def test_sync_models_staging_failure_restores_previous_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _model_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.pkl").write_bytes(b"old")
    real_replace = os.replace

    def fail_publish(source_path, destination_path):
        source_name = Path(source_path).name
        if source_name.startswith(".destination.staging-"):
            raise OSError("publish failed")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(publish_deployment_cache.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        publish_deployment_cache.sync_models(source, destination)

    assert sorted(path.name for path in destination.iterdir()) == ["old.pkl"]
    assert (destination / "old.pkl").read_bytes() == b"old"
    assert not list(tmp_path.glob(".destination.*"))


def test_sync_models_explicit_prune_removes_source_external_entries(tmp_path: Path) -> None:
    source = _model_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.pkl").write_bytes(b"old")
    (destination / "old_dir").mkdir()

    copied = publish_deployment_cache.sync_models(source, destination, prune=True)

    assert [path.name for path in copied] == ["model.pkl", "thresholds.json"]
    assert sorted(path.name for path in destination.iterdir()) == [
        "model.pkl",
        "thresholds.json",
    ]


def test_sync_main_supports_explicit_prune(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _model_source(tmp_path)
    destination = tmp_path / "published"

    result = publish_deployment_cache.main(
        ["--source", str(source), "--destination", str(destination), "--prune"]
    )

    assert result == 0
    assert (destination / "model.pkl").read_bytes() == b"new model"
    output = capsys.readouterr().out
    assert str(source.resolve()) in output
    assert str(destination.resolve()) in output
    assert "(prune)" in output


def test_sync_models_rejects_empty_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (destination / "old.pkl").write_bytes(b"old")

    with pytest.raises(ValueError, match="no supported model files"):
        publish_deployment_cache.sync_models(source, destination)

    assert (destination / "old.pkl").read_bytes() == b"old"


@pytest.mark.parametrize("destination_kind", ["same", "inside"])
def test_sync_models_rejects_destination_overlapping_source(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    source = _model_source(tmp_path)
    destination = source if destination_kind == "same" else source / "published"

    with pytest.raises(ValueError, match="same directory|must not overlap"):
        publish_deployment_cache.sync_models(source, destination)

    assert not (source / "published").exists()


def test_sync_models_rejects_source_inside_destination(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    source = destination / "source"
    source.mkdir(parents=True)
    (source / "model.pkl").write_bytes(b"model")

    with pytest.raises(ValueError, match="must not overlap"):
        publish_deployment_cache.sync_models(source, destination)


def test_sync_models_rejects_repository_destination(tmp_path: Path) -> None:
    source = _model_source(tmp_path)
    destination = publish_deployment_cache.REPOSITORY_ROOT / "data" / "models"

    with pytest.raises(ValueError, match="outside the repository"):
        publish_deployment_cache.sync_models(source, destination)


def test_sync_models_rejects_candidate_promotion_into_current(tmp_path: Path) -> None:
    source = tmp_path / "models" / "candidates" / "supervised" / "release-a"
    source.mkdir(parents=True)
    (source / "model.pkl").write_bytes(b"model")
    destination = tmp_path / "models" / "current" / "release-a"

    with pytest.raises(ValueError, match="models/current"):
        publish_deployment_cache.sync_models(source, destination)

    assert not destination.exists()


def test_validate_cleanup_root_uses_complete_layout(cleanup_root: Path) -> None:
    unresolved = cleanup_root / "models" / ".."
    assert gdisk_cleanup.validate_cleanup_root(unresolved) == cleanup_root.resolve()

    invalid = cleanup_root.parent / "invalid"
    invalid.mkdir()
    with pytest.raises(ValueError, match="missing directories"):
        gdisk_cleanup.validate_cleanup_root(invalid)


def test_validate_cleanup_root_rejects_drive_or_filesystem_root() -> None:
    filesystem_root = Path(Path.cwd().anchor)
    with pytest.raises(ValueError, match="drive or filesystem root"):
        gdisk_cleanup.validate_cleanup_root(filesystem_root)


def test_cleanup_dry_run_writes_plan_without_selecting_broad_candidates(
    cleanup_root: Path,
) -> None:
    obsolete = cleanup_root / "models" / "rl" / "candidates" / "run_rl_obs_v1"
    obsolete.mkdir()

    summary = gdisk_cleanup.run_cleanup(cleanup_root, run_id="dry-run")

    manifest = Path(summary["manifest_path"])
    assert summary["apply"] is False
    assert summary["operations"] == []
    assert obsolete.exists()
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "planned"
    assert not (cleanup_root / "quarantine").exists()


def test_cleanup_rejects_research_reports_even_when_explicit(
    cleanup_root: Path,
) -> None:
    report = cleanup_root / "experiments" / "reports" / "rl" / "result.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="research reports"):
        gdisk_cleanup.run_cleanup(
            cleanup_root,
            allowlist=["experiments/reports/rl/result.json"],
            run_id="protected-report",
        )

    assert report.exists()
    assert not (cleanup_root / "manifests" / "cleanup" / "protected-report.json").exists()


def test_cleanup_rejects_overlapping_allowlist_entries(cleanup_root: Path) -> None:
    selected = cleanup_root / "models" / "archive" / "old-release"
    (selected / "nested").mkdir(parents=True)

    with pytest.raises(ValueError, match="must not overlap"):
        gdisk_cleanup.run_cleanup(
            cleanup_root,
            allowlist=[
                "models/archive/old-release",
                "models/archive/old-release/nested",
            ],
            run_id="overlap",
        )

    assert selected.is_dir()
    assert not (cleanup_root / "manifests" / "cleanup" / "overlap.json").exists()


def test_cleanup_apply_moves_only_allowlisted_target_to_quarantine(
    cleanup_root: Path,
) -> None:
    selected = cleanup_root / "models" / "rl" / "candidates" / "selected"
    selected.mkdir()
    (selected / "model.zip").write_bytes(b"model")
    untouched = cleanup_root / "models" / "rl" / "candidates" / "untouched"
    untouched.mkdir()

    summary = gdisk_cleanup.run_cleanup(
        cleanup_root,
        allowlist=["models/rl/candidates/selected"],
        apply_changes=True,
        run_id="apply-one",
    )

    quarantined = (
        cleanup_root
        / "quarantine"
        / "apply-one"
        / "models"
        / "rl"
        / "candidates"
        / "selected"
    )
    assert summary["status"] == "completed"
    assert not selected.exists()
    assert quarantined.is_dir()
    assert untouched.is_dir()
    manifest = json.loads(Path(summary["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["operations"][0]["status"] == "moved"


def test_cleanup_persists_intent_before_move(
    cleanup_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = cleanup_root / "models" / "archive" / "old-release"
    selected.mkdir()
    real_move = shutil.move

    def inspect_then_move(source, destination):
        manifest = cleanup_root / "manifests" / "cleanup" / "intent-first.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["status"] == "applying"
        assert payload["operations"][0]["status"] == "moving"
        assert Path(source).exists()
        return real_move(source, destination)

    monkeypatch.setattr(gdisk_cleanup.shutil, "move", inspect_then_move)
    gdisk_cleanup.run_cleanup(
        cleanup_root,
        allowlist=["models/archive/old-release"],
        apply_changes=True,
        run_id="intent-first",
    )


def test_cleanup_failure_is_audited_and_does_not_continue(
    cleanup_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = cleanup_root / "models" / "archive" / "a-release"
    second = cleanup_root / "models" / "archive" / "b-release"
    first.mkdir()
    second.mkdir()
    real_move = shutil.move
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("blocked move")
        return real_move(source, destination)

    monkeypatch.setattr(gdisk_cleanup.shutil, "move", fail_second)
    with pytest.raises(PermissionError, match="blocked move"):
        gdisk_cleanup.run_cleanup(
            cleanup_root,
            allowlist=["models/archive/a-release", "models/archive/b-release"],
            apply_changes=True,
            run_id="partial-failure",
        )

    manifest_path = cleanup_root / "manifests" / "cleanup" / "partial-failure.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert [item["status"] for item in manifest["operations"]] == ["moved", "failed"]
    assert "PermissionError" in manifest["operations"][1]["error"]
    assert not first.exists()
    assert second.exists()


def test_cleanup_rejects_manifest_outside_root_before_moving(
    cleanup_root: Path,
) -> None:
    selected = cleanup_root / "models" / "archive" / "old-release"
    selected.mkdir()

    with pytest.raises(ValueError, match="escapes cleanup root"):
        gdisk_cleanup.run_cleanup(
            cleanup_root,
            allowlist=["models/archive/old-release"],
            apply_changes=True,
            manifest_path=cleanup_root.parent / "outside.json",
            run_id="outside-manifest",
        )

    assert selected.exists()
