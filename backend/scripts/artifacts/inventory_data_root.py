"""Build a deterministic inventory for a CandleMind data root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.app.data_layout import REQUIRED_DIRECTORIES, validate_data_root as _validate_data_root


def validate_data_root(root: Path) -> Path:
    return _validate_data_root(root)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _category(relative: Path) -> str:
    parts = relative.parts
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def build_inventory(
    root: Path,
    *,
    include_hashes: bool = False,
    excluded_paths: tuple[Path, ...] = (),
) -> dict:
    root = validate_data_root(root)
    excluded = {path.expanduser().resolve() for path in excluded_paths}
    entries = []
    empty_directories = []
    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    hashes: dict[str, list[str]] = defaultdict(list)

    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        current = Path(directory)
        if current != root and not dirnames and not filenames:
            empty_directories.append(current.relative_to(root).as_posix())
        for filename in filenames:
            path = current / filename
            if path.resolve() in excluded:
                continue
            stat = path.stat()
            relative = path.relative_to(root)
            entry = {
                "path": relative.as_posix(),
                "bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
            if include_hashes:
                entry["sha256"] = sha256_file(path)
                hashes[entry["sha256"]].append(entry["path"])
            entries.append(entry)
            bucket = categories[_category(relative)]
            bucket["files"] += 1
            bucket["bytes"] += stat.st_size

    duplicates = [paths for paths in hashes.values() if len(paths) > 1]
    model_releases = {}
    current_models = root / "models" / "current"
    releases_dir = root / "models" / "releases"
    active_file = current_models / "ACTIVE"
    active_release = (
        active_file.read_text(encoding="utf-8").strip()
        if active_file.is_file()
        else None
    )
    for release_root in (releases_dir, current_models):
        if not release_root.is_dir():
            continue
        for release in sorted(path for path in release_root.iterdir() if path.is_dir()):
            if release.name in model_releases:
                continue
            relative_root = release_root.relative_to(root).as_posix()
            release_entries = [
                entry for entry in entries
                if entry["path"].startswith(f"{relative_root}/{release.name}/")
            ]
            model_releases[release.name] = {
                "path": f"{relative_root}/{release.name}",
                "active": release.name == active_release or (
                    active_release is None and release_root == current_models
                ),
                "files": len(release_entries),
                "bytes": sum(entry["bytes"] for entry in release_entries),
                "latest_mtime_utc": max(
                    (entry["mtime_utc"] for entry in release_entries), default=None
                ),
            }

    return {
        "schema": "candlemind-data-inventory-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "hash_algorithm": "sha256" if include_hashes else None,
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "categories": dict(sorted(categories.items())),
        "model_releases": model_releases,
        "active_model_release": active_release,
        "empty_directories": sorted(empty_directories),
        "duplicate_hash_groups": duplicates,
        "files": entries,
    }


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hash", action="store_true", dest="include_hashes")
    args = parser.parse_args()

    output = args.output
    excluded = (output,) if output is not None else ()
    inventory = build_inventory(
        args.root,
        include_hashes=args.include_hashes,
        excluded_paths=excluded,
    )
    if output is None:
        print(json.dumps({key: value for key, value in inventory.items() if key != "files"}, indent=2))
        return
    write_json_atomic(output, inventory)
    print(f"Inventory written: {output} ({inventory['file_count']} files)")


if __name__ == "__main__":
    main()
