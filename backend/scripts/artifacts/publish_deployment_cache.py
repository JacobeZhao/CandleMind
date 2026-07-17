"""Publish current trained models into an explicit external deployment cache."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_SUFFIXES = frozenset({".json", ".pkl"})


def default_source() -> Path:
    """Resolve the datastore model release only when the CLI is executed."""
    try:
        from backend.app.datastore import resolve_current_model_release
    except ModuleNotFoundError:
        from app.datastore import resolve_current_model_release

    return resolve_current_model_release()


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy one file and atomically publish it at destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _containing_models_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if candidate.name.casefold() == "models":
            return candidate
    return None


def _validated_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise NotADirectoryError(f"model source is not a directory: {source}")
    if source == Path(source.anchor):
        raise ValueError(f"unsafe model sync source: {source}")

    destination = destination.expanduser().resolve(strict=False)
    if destination == Path(destination.anchor):
        raise ValueError(f"unsafe model sync destination: {destination}")
    if source == destination:
        raise ValueError(
            "unsafe model sync: source and destination resolve to the same "
            f"directory: {source}"
        )
    if _contains(source, destination) or _contains(destination, source):
        raise ValueError(
            "unsafe model sync: source and destination directories must not overlap "
            f"(source={source}, destination={destination})"
        )
    models_root = _containing_models_root(source)
    if models_root is not None and _contains(models_root / "current", destination):
        raise ValueError(
            "deployment cache destination cannot be under models/current: "
            f"{destination}"
        )
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(f"model destination is not a directory: {destination}")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if _contains(repository_root, destination):
        raise ValueError(
            "model destination must be outside the repository: "
            f"{destination}"
        )
    return source, destination


def _model_files(source: Path, suffixes: frozenset[str]) -> list[Path]:
    files = [
        candidate
        for candidate in sorted(source.iterdir(), key=lambda path: path.name)
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.suffix.lower() in suffixes
    ]
    if not files:
        raise ValueError(f"model source contains no supported model files: {source}")
    return files


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _sync_with_prune(files: list[Path], destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    expected = {source.name for source in files}
    published = []
    for source in files:
        target = destination / source.name
        atomic_copy(source, target)
        published.append(target)
    for stale in list(destination.iterdir()):
        if stale.name not in expected:
            _remove_path(stale)
    return published


def _sync_with_staging(files: list[Path], destination: Path) -> list[Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = destination.parent / f".{destination.name}.staging-{token}"
    backup = destination.parent / f".{destination.name}.backup-{token}"
    staging.mkdir()
    backup_created = False
    try:
        for source in files:
            shutil.copy2(source, staging / source.name)

        if destination.exists():
            os.replace(destination, backup)
            backup_created = True
        try:
            os.replace(staging, destination)
        except Exception:
            if backup_created:
                os.replace(backup, destination)
                backup_created = False
            raise

        if backup_created:
            shutil.rmtree(backup)
            backup_created = False
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup_created and backup.exists() and not destination.exists():
            os.replace(backup, destination)

    return [destination / source.name for source in files]


def sync_models(
    source: Path,
    destination: Path,
    *,
    suffixes: frozenset[str] = MODEL_SUFFIXES,
    prune: bool = False,
) -> list[Path]:
    """Publish an exact top-level model set, using staging by default."""
    source, destination = _validated_paths(source, destination)
    files = _model_files(source, suffixes)
    if prune:
        return _sync_with_prune(files, destination)
    return _sync_with_staging(files, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Model release directory (default: resolved active release)",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="External deployment cache; repository paths are rejected",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Update in place and explicitly remove entries absent from source",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source if args.source is not None else default_source()
    copied = sync_models(source, args.destination, prune=args.prune)
    resolved_source = source.expanduser().resolve(strict=True)
    resolved_destination = args.destination.expanduser().resolve(strict=False)
    mode = "prune" if args.prune else "staging-swap"
    print(
        f"Published {len(copied)} model files from {resolved_source} "
        f"to {resolved_destination} ({mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
