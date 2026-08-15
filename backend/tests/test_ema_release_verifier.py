from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
import sys
from typing import Any, Callable
import zipfile

import pandas as pd
import pytest

from backend.app.services.ema_data_release import (
    verify_ema_data_release as verify_legacy_release,
)
from backend.app.services.ema_release_verifier import (
    EmaDataReleaseError,
    _is_absolute_source_path,
    _uses_current_alias,
    verify_ema_data_release,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ema_release_v2"
ARCHIVE = FIXTURE_DIR / "ema_release_v2_minimal.zip"
SIDECAR = FIXTURE_DIR / "ema_release_v2_minimal.fixture.json"
VERIFIER_SOURCE = (
    Path(__file__).parents[1] / "app" / "services" / "ema_release_verifier.py"
)
MAX_ARCHIVE_FILES = 32
MAX_MEMBER_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024
UNIVERSE_GOLDEN_SHA256 = (
    "97faf9c2f5e45c2b41dc8b2f0372ecefa1594572e44901932bf59bbed849af79"
)


@pytest.mark.parametrize(
    "source_path",
    [
        "/srv/candlemind/source.parquet",
        r"G:\CandleMind\source.parquet",
        r"\\server\share\source.parquet",
    ],
    ids=["posix", "windows-drive", "windows-unc"],
)
def test_source_path_recognizes_cross_platform_absolute_paths(source_path: str) -> None:
    assert _is_absolute_source_path(source_path)


@pytest.mark.parametrize(
    "source_path",
    ["source.parquet", "data/source.parquet", r"data\source.parquet", r"G:source.parquet"],
    ids=["name", "posix-relative", "windows-relative", "drive-relative"],
)
def test_source_path_rejects_relative_paths(source_path: str) -> None:
    assert not _is_absolute_source_path(source_path)


@pytest.mark.parametrize(
    "source_path",
    [
        "/srv/current/source.parquet",
        r"G:\CandleMind\CURRENT\source.parquet",
        r"\\server\share\current.json\source.parquet",
    ],
    ids=["posix", "windows", "unc-with-suffix"],
)
def test_source_path_detects_current_alias_in_both_path_flavours(
    source_path: str,
) -> None:
    assert _uses_current_alias(source_path)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _object_hash(value: dict[str, Any], excluded: str) -> str:
    unsigned = dict(value)
    unsigned.pop(excluded, None)
    return _sha256_bytes(_canonical_json(unsigned))


def _source_id(record: dict[str, Any]) -> str:
    schema = record.get("schema")
    identity = {
        "kind": record.get("kind"),
        "symbol": record.get("symbol"),
        "sha256": record.get("sha256"),
        "semantic_sha256": record.get("semantic_sha256"),
        "schema_sha256": schema.get("sha256") if isinstance(schema, dict) else None,
        "window": record.get("window"),
    }
    return _sha256_bytes(_canonical_json(identity))


def _source_tree_hash(records: list[dict[str, Any]]) -> str:
    identity = [
        {
            "repository_path": record.get("repository_path"),
            "bytes": record.get("bytes"),
            "sha256": record.get("sha256"),
        }
        for record in records
    ]
    identity.sort(key=lambda item: str(item["repository_path"]))
    return _sha256_bytes(_canonical_json(identity))


def _release_digest(manifest: dict[str, Any]) -> str:
    outputs = []
    for record in manifest.get("outputs", []):
        schema = record.get("schema")
        outputs.append(
            {
                "kind": record.get("kind"),
                "symbol": record.get("symbol"),
                "source_id": record.get("source_id"),
                "bytes": record.get("bytes"),
                "sha256": record.get("sha256"),
                "semantic_sha256": record.get("semantic_sha256"),
                "schema_sha256": (
                    schema.get("sha256") if isinstance(schema, dict) else None
                ),
                "window": record.get("window"),
            }
        )
    outputs.sort(key=lambda item: (str(item["kind"]), str(item["symbol"])))
    pit = manifest.get("pit_evidence", {})
    universe_evidence = pit.get("pit_universe_manifest", {})
    readiness_evidence = pit.get("pit_readiness_audit", {})
    identity = {
        "schema": manifest.get("schema"),
        "window": manifest.get("window"),
        "coverage": manifest.get("coverage"),
        "universe": manifest.get("universe"),
        "pit_evidence": {
            "universe_file_sha256": universe_evidence.get("sha256"),
            "universe_manifest_sha256": universe_evidence.get("manifest_sha256"),
            "universe_release_id": universe_evidence.get("release_id"),
            "readiness_file_sha256": readiness_evidence.get("sha256"),
            "readiness_report_sha256": readiness_evidence.get("report_sha256"),
        },
        "outputs": outputs,
        "source_tree_sha256": manifest.get("source_snapshot", {}).get(
            "source_tree_sha256"
        ),
    }
    return _sha256_bytes(_canonical_json(identity))


def _sign_manifest(manifest: dict[str, Any]) -> None:
    manifest["release_digest"] = _release_digest(manifest)
    manifest["manifest_sha256"] = _object_hash(manifest, "manifest_sha256")


def _write_manifest(release: Path, manifest: dict[str, Any]) -> None:
    _sign_manifest(manifest)
    (release / "manifest.json").write_bytes(_canonical_json(manifest))


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise ValueError("archive has too many members")
        total = 0
        seen: set[str] = set()
        for member in members:
            name = member.filename
            original_name = member.orig_filename
            if not name or "\\" in original_name:
                raise ValueError("archive member uses an invalid separator")
            path = PurePosixPath(name)
            windows_path = PureWindowsPath(name)
            if (
                path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
            ):
                raise ValueError("archive member escapes destination")
            key = path.as_posix().casefold()
            if key in seen:
                raise ValueError("archive contains duplicate paths")
            seen.add(key)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("archive contains a symbolic link")
            if member.file_size > MAX_MEMBER_BYTES:
                raise ValueError("archive member exceeds size limit")
            total += member.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("archive exceeds expanded size limit")
            target = (root / Path(*path.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError("archive member escapes destination") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                while block := source.read(64 * 1024):
                    output.write(block)


@pytest.fixture
def release(tmp_path: Path) -> Path:
    target = tmp_path / "release"
    _safe_extract(ARCHIVE, target)
    return target


def _inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def _file_state(root: Path) -> dict[str, tuple[int, str, int]]:
    return {
        record["path"]: (
            record["bytes"],
            record["sha256"],
            (root / record["path"]).stat().st_mtime_ns,
        )
        for record in _inventory(root)
    }


def _make_zip(path: Path, members: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        for member, payload in members:
            if isinstance(member, str) and "\\" in member:
                raw_member = zipfile.ZipInfo("placeholder")
                raw_member.filename = member
                raw_member.orig_filename = member
                member = raw_member
            bundle.writestr(member, payload)


def test_fixture_archive_and_extracted_inventory_match_sidecar(
    release: Path,
) -> None:
    sidecar = json.loads(SIDECAR.read_text(encoding="ascii"))

    assert ARCHIVE.stat().st_size == sidecar["archive"]["bytes"]
    assert _sha256_file(ARCHIVE) == sidecar["archive"]["sha256"]
    assert _inventory(release) == sidecar["files"]


@pytest.mark.parametrize(
    "members",
    [
        [("../escape", b"x")],
        [("/absolute", b"x")],
        [("C:/absolute", b"x")],
        [("dir\\escape", b"x")],
        [("same", b"a"), ("SAME", b"b")],
    ],
    ids=["traversal", "posix-absolute", "drive-absolute", "backslash", "case-collision"],
)
def test_safe_extraction_rejects_unsafe_paths(
    tmp_path: Path,
    members: list[tuple[str, bytes]],
) -> None:
    archive = tmp_path / "unsafe.zip"
    _make_zip(archive, members)

    with pytest.raises(ValueError):
        _safe_extract(archive, tmp_path / "output")


def test_safe_extraction_rejects_exact_duplicate(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        _make_zip(archive, [("same", b"a"), ("same", b"b")])

    with pytest.raises(ValueError, match="duplicate"):
        _safe_extract(archive, tmp_path / "output")


def test_safe_extraction_rejects_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _make_zip(archive, [(link, b"target")])

    with pytest.raises(ValueError, match="symbolic link"):
        _safe_extract(archive, tmp_path / "output")


@pytest.mark.parametrize("limit", ["members", "member-bytes", "total-bytes"])
def test_safe_extraction_enforces_limits(tmp_path: Path, limit: str) -> None:
    archive = tmp_path / "oversized.zip"
    if limit == "members":
        members = [(f"file-{index}", b"x") for index in range(MAX_ARCHIVE_FILES + 1)]
    elif limit == "member-bytes":
        members = [("large", b"x" * (MAX_MEMBER_BYTES + 1))]
    else:
        chunk = b"x" * MAX_MEMBER_BYTES
        members = [
            (f"part-{index}", chunk)
            for index in range(MAX_ARCHIVE_BYTES // MAX_MEMBER_BYTES + 1)
        ]
    _make_zip(archive, members)

    with pytest.raises(ValueError, match="limit|too many"):
        _safe_extract(archive, tmp_path / "output")


def test_valid_fixture_has_legacy_parity_and_golden_hashes(release: Path) -> None:
    sidecar = json.loads(SIDECAR.read_text(encoding="ascii"))
    frozen_manifest = json.loads((release / "manifest.json").read_text(encoding="ascii"))

    legacy = verify_legacy_release(release)
    current = verify_ema_data_release(release)

    assert current == legacy == frozen_manifest
    assert current["manifest_sha256"] == sidecar["manifest_sha256"]
    assert current["release_digest"] == sidecar["release_digest"]
    assert current["source_snapshot"]["source_tree_sha256"] == sidecar[
        "source_tree_sha256"
    ]
    assert current["universe"]["content_sha256"] == UNIVERSE_GOLDEN_SHA256
    assert current["universe"]["content_sha256"] == sidecar[
        "universe_content_sha256"
    ]


def test_normalized_source_paths_are_absolute_for_windows_and_posix(
    release: Path,
) -> None:
    manifest = json.loads((release / "manifest.json").read_text(encoding="ascii"))
    readiness_path = release / manifest["pit_evidence"]["pit_readiness_audit"]["path"]
    readiness = json.loads(readiness_path.read_text(encoding="ascii"))
    source_paths = [record["path"] for record in manifest["inputs"]]
    source_paths.extend(
        record["source_path"] for record in manifest["pit_evidence"].values()
    )
    source_paths.extend(
        readiness["components"]["pit_universe"]["evidence_paths"]
    )

    assert source_paths
    for source_path in source_paths:
        assert PureWindowsPath(source_path).is_absolute(), source_path
        assert PurePosixPath(source_path).is_absolute(), source_path


def test_verification_is_byte_and_metadata_read_only(release: Path) -> None:
    before = _file_state(release)

    verify_ema_data_release(release)

    assert _file_state(release) == before


def test_import_is_isolated_from_legacy_builder_and_rl_modules() -> None:
    code = """
import json
import os
import sys
sys.path.insert(0, os.getcwd())
import backend.app.services.ema_release_verifier
forbidden = sorted(name for name in sys.modules if (
    name == 'backend.app.services.ema_data_release'
    or name == 'backend.app.services.point_in_time_universe'
    or name == 'backend.scripts.data.build_ema_data_release'
    or name == 'backend.app.rl'
    or name.startswith('backend.app.rl.')
))
print(json.dumps(forbidden))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_verifier_static_boundary_excludes_publishers_and_write_operations() -> None:
    tree = ast.parse(VERIFIER_SOURCE.read_text(encoding="utf-8"))
    forbidden_imports = {
        "backend.app.services.ema_data_release",
        "backend.app.services.point_in_time_universe",
        "backend.scripts.data.build_ema_data_release",
        "backend.app.rl",
        "os",
        "shutil",
        "subprocess",
        "tempfile",
        "uuid",
    }
    imports: set[str] = set()
    writes: list[str] = []
    publisher_functions: list[str] = []
    write_methods = {
        "mkdir",
        "open",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith(("build", "copy", "publish", "write")):
                publisher_functions.append(node.name)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in write_methods:
                if node.func.attr != "open":
                    writes.append(node.func.attr)
                elif not (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "rb"
                ):
                    writes.append("open")

    forbidden_loaded = {
        imported
        for imported in imports
        if imported in forbidden_imports
        or imported.startswith("backend.app.rl.")
    }
    assert forbidden_loaded == set()
    assert publisher_functions == []
    assert writes == []


def _tamper_coverage(release: Path, manifest: dict[str, Any]) -> None:
    manifest["coverage"]["eligible_pair_count"] = 2


def _tamper_universe_binding(release: Path, manifest: dict[str, Any]) -> None:
    manifest["universe"]["content_sha256"] = "f" * 64


def _tamper_source_path(release: Path, manifest: dict[str, Any]) -> None:
    manifest["source_snapshot"]["files"][0]["path"] = "source/unregistered.py"


def _tamper_source_tree(release: Path, manifest: dict[str, Any]) -> None:
    records = copy.deepcopy(manifest["source_snapshot"]["files"])
    records[0]["bytes"] += 1
    manifest["source_snapshot"]["source_tree_sha256"] = _source_tree_hash(records)


def _tamper_semantic_parquet(release: Path, manifest: dict[str, Any]) -> None:
    output = next(record for record in manifest["outputs"] if record["kind"] == "ohlcv")
    path = release / output["path"]
    frame = pd.read_parquet(path)
    frame.loc[0, "close"] = float(frame.loc[0, "close"]) + 0.25
    frame.to_parquet(path, index=False)
    changed_size = path.stat().st_size
    changed_sha = _sha256_file(path)
    source = next(
        record for record in manifest["inputs"] if record["source_id"] == output["source_id"]
    )
    for record in (source, output):
        record["bytes"] = changed_size
        record["sha256"] = changed_sha
    changed_source_id = _source_id(source)
    source["source_id"] = changed_source_id
    output["source_id"] = changed_source_id


def _tamper_extra_file(release: Path, manifest: dict[str, Any]) -> None:
    (release / "unregistered.txt").write_text("extra", encoding="ascii")


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_tamper_coverage, "eligible_pair_count verification failed"),
        (_tamper_universe_binding, "universe content hash verification failed"),
        (_tamper_source_path, "source snapshot path is invalid"),
        (_tamper_source_tree, "source_tree_sha256 verification failed"),
        (_tamper_semantic_parquet, "semantic hash verification failed"),
        (_tamper_extra_file, "missing or unregistered files"),
    ],
    ids=[
        "coverage",
        "universe-binding",
        "source-path",
        "source-tree",
        "semantic-parquet",
        "extra-file",
    ],
)
def test_independently_resigned_deep_tampering_is_rejected(
    release: Path,
    tamper: Callable[[Path, dict[str, Any]], None],
    message: str,
) -> None:
    manifest = json.loads((release / "manifest.json").read_text(encoding="ascii"))
    tamper(release, manifest)
    _write_manifest(release, manifest)

    persisted = json.loads((release / "manifest.json").read_text(encoding="ascii"))
    assert persisted["manifest_sha256"] == _object_hash(
        persisted, "manifest_sha256"
    )
    assert persisted["release_digest"] == _release_digest(persisted)
    with pytest.raises(EmaDataReleaseError, match=message):
        verify_ema_data_release(release)
