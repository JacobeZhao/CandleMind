from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.scripts.artifacts import rebuild_rl_registry


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "CandleMind_data"
    (root / "models" / "rl" / "candidates").mkdir(parents=True)
    return root


def _write_manifest(
    root: Path,
    run_path: str,
    *,
    created_at: str,
    status: str = "candidate",
    model_id: str | None = None,
) -> Path:
    run_dir = root / "models" / "rl" / "candidates" / run_path
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "model_id": model_id or run_dir.name,
                "created_at": created_at,
                "status": status,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_rebuild_is_deterministic_and_uses_data_root_relative_paths(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_manifest(
        root,
        "z-group/run-z",
        created_at="2026-07-16T02:00:00+00:00",
        status="rejected",
    )
    _write_manifest(
        root,
        "a-group/run-a",
        created_at="2026-07-16T01:00:00+00:00",
    )

    first = rebuild_rl_registry.rebuild_registry(root)
    first_bytes = (root / "models" / "rl" / "registry.json").read_bytes()
    second = rebuild_rl_registry.rebuild_registry(root)

    assert first == second == {
        "version": "rl_registry_v1",
        "entries": [
            {
                "created_at": "2026-07-16T01:00:00+00:00",
                "manifest_path": "models/rl/candidates/a-group/run-a/manifest.json",
                "model_id": "run-a",
                "run_dir": "models/rl/candidates/a-group/run-a",
                "status": "candidate",
            },
            {
                "created_at": "2026-07-16T02:00:00+00:00",
                "manifest_path": "models/rl/candidates/z-group/run-z/manifest.json",
                "model_id": "run-z",
                "run_dir": "models/rl/candidates/z-group/run-z",
                "status": "rejected",
            },
        ],
    }
    assert (root / "models" / "rl" / "registry.json").read_bytes() == first_bytes


def test_atomic_write_replaces_registry_from_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _data_root(tmp_path)
    _write_manifest(root, "run-a", created_at="2026-07-16T01:00:00Z")
    registry = root / "models" / "rl" / "registry.json"
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(rebuild_rl_registry.os, "replace", recording_replace)

    rebuild_rl_registry.rebuild_registry(root)

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == registry.parent
    assert destination == registry
    assert not temporary.exists()


def test_check_validates_without_requiring_registry_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _data_root(tmp_path)
    _write_manifest(root, "run-a", created_at="2026-07-16T01:00:00+00:00")
    registry = root / "models" / "rl" / "registry.json"

    def fail_write(*args, **kwargs) -> None:
        raise AssertionError("--check attempted to write")

    monkeypatch.setattr(rebuild_rl_registry, "write_registry_atomic", fail_write)

    assert rebuild_rl_registry.main(["--root", str(root), "--check"]) == 0
    assert not registry.exists()


def test_check_does_not_replace_an_existing_registry(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    _write_manifest(root, "run-a", created_at="2026-07-16T01:00:00+00:00")
    registry = root / "models" / "rl" / "registry.json"
    registry.write_text('{"version":"stale","entries":[]}', encoding="utf-8")
    before = registry.read_bytes()

    assert rebuild_rl_registry.main(["--root", str(root), "--check"]) == 0
    assert registry.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"created_at": "2026-07-16T01:00:00+00:00", "status": "candidate"}, "model_id"),
        (
            {
                "model_id": "different-run",
                "created_at": "2026-07-16T01:00:00+00:00",
                "status": "candidate",
            },
            "run directory name",
        ),
        (
            {
                "model_id": "run-a",
                "created_at": "2026-07-16T01:00:00+00:00",
                "status": "unknown",
            },
            "status",
        ),
        (
            {"model_id": "run-a", "created_at": "not-a-date", "status": "candidate"},
            "ISO-8601",
        ),
    ],
)
def test_manifest_fields_are_strictly_validated(
    tmp_path: Path, payload: object, message: str
) -> None:
    root = _data_root(tmp_path)
    run_dir = root / "models" / "rl" / "candidates" / "run-a"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(rebuild_rl_registry.RegistryValidationError, match=message):
        rebuild_rl_registry.build_registry(root)


def test_duplicate_model_ids_are_rejected(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    first = _write_manifest(root, "group-a/run", created_at="2026-07-16T01:00:00Z")
    second = _write_manifest(root, "group-b/run", created_at="2026-07-16T02:00:00Z")
    assert first.parent.name == second.parent.name

    with pytest.raises(rebuild_rl_registry.RegistryValidationError, match="duplicate"):
        rebuild_rl_registry.build_registry(root)


def test_manifest_symlink_cannot_escape_candidates(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "model_id": "run-a",
                "created_at": "2026-07-16T01:00:00Z",
                "status": "candidate",
            }
        ),
        encoding="utf-8",
    )
    run_dir = root / "models" / "rl" / "candidates" / "run-a"
    run_dir.mkdir()
    try:
        (run_dir / "manifest.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(rebuild_rl_registry.RegistryValidationError, match="escapes"):
        rebuild_rl_registry.build_registry(root)


def test_root_resolution_prefers_explicit_then_environment_without_io(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"

    assert rebuild_rl_registry.resolve_data_root(
        explicit,
        environ={"MARKET_DATA_DIR": str(configured)},
        platform="win32",
    ) == explicit
    assert rebuild_rl_registry.resolve_data_root(
        environ={"MARKET_DATA_DIR": str(configured)},
        platform="win32",
    ) == configured
    assert rebuild_rl_registry.resolve_data_root(environ={}, platform="win32") == Path(
        "G:/CandleMind/CandleMind_data"
    )
