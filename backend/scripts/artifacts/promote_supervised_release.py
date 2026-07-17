"""Validate and atomically activate a sealed supervised model release."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from backend.app import datastore
from backend.scripts.artifacts.inventory_data_root import sha256_file


MANIFEST_NAME = "release_manifest.json"


def validate_candidate_release(candidate: Path) -> dict:
    candidate = datastore.validate_supervised_candidate_dir(candidate)
    manifest_path = candidate / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    if payload.get("schema") != "candlemind-model-release-v1":
        raise ValueError("unsupported release manifest schema")
    if payload.get("release_id") != candidate.name:
        raise ValueError("manifest release_id does not match candidate directory")
    if payload.get("immutable") is not True:
        raise ValueError("release manifest must be immutable")
    if payload.get("status") != "documented":
        raise ValueError("release lineage is incomplete")
    if payload.get("lineage", {}).get("complete") is not True:
        raise ValueError("release lineage is incomplete")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("release manifest contains no artifacts")
    by_name: dict[str, dict] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("invalid artifact entry")
        name = artifact.get("name")
        if not isinstance(name, str) or Path(name).name != name or name in by_name:
            raise ValueError(f"invalid artifact name: {name!r}")
        by_name[name] = artifact

    candidate_entries = [path for path in candidate.iterdir() if path.name != MANIFEST_NAME]
    unsafe_entries = [
        path.name for path in candidate_entries if path.is_symlink() or not path.is_file()
    ]
    if unsafe_entries:
        raise ValueError(f"release contains unsafe entries: {sorted(unsafe_entries)}")
    actual = {path.name: path for path in candidate_entries}
    if set(actual) != set(by_name):
        raise ValueError("release artifacts do not match the sealed manifest")
    for name, path in actual.items():
        artifact = by_name[name]
        if path.stat().st_size != artifact.get("bytes"):
            raise ValueError(f"artifact size mismatch: {name}")
        if sha256_file(path) != artifact.get("sha256"):
            raise ValueError(f"artifact hash mismatch: {name}")
    return payload


@contextmanager
def _promotion_lock():
    lock_path = datastore.MODELS_ROOT / ".promote-supervised.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _activate_release(release_id: str) -> None:
    datastore.MODELS_CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ACTIVE.", suffix=".tmp", dir=datastore.MODELS_CURRENT_DIR
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{release_id}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, datastore.ACTIVE_MODEL_RELEASE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def promote_release(release_id: str) -> Path:
    candidate = datastore.supervised_candidate_dir(release_id)
    validate_candidate_release(candidate)
    destination = datastore.MODELS_RELEASES_DIR / release_id

    with _promotion_lock():
        if destination.exists():
            raise FileExistsError(f"release already exists: {destination}")
        datastore.MODELS_RELEASES_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, destination)
        _activate_release(release_id)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    destination = promote_release(args.release_id)
    print(f"Promoted supervised release: {destination}")


if __name__ == "__main__":
    main()
