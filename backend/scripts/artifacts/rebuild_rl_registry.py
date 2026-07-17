"""Deterministically rebuild the RL candidate registry from run manifests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


REGISTRY_VERSION = "rl_registry_v1"
DEFAULT_WINDOWS_DATA_ROOT = Path("G:/CandleMind/CandleMind_data")
ALLOWED_STATUSES = frozenset({"candidate", "rejected"})


class RegistryValidationError(ValueError):
    """Raised when the data layout or an RL manifest is unsafe or invalid."""


def resolve_data_root(
    root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve an explicit root, ``MARKET_DATA_DIR``, or the Windows default."""
    if root is not None:
        return root

    environment = os.environ if environ is None else environ
    configured = environment.get("MARKET_DATA_DIR")
    if configured is not None:
        if not configured.strip():
            raise RegistryValidationError("MARKET_DATA_DIR is set but empty")
        return Path(configured)

    platform_name = sys.platform if platform is None else platform
    if platform_name.lower().startswith("win"):
        return DEFAULT_WINDOWS_DATA_ROOT
    raise RegistryValidationError(
        "a data root is required outside Windows; pass --root or set MARKET_DATA_DIR"
    )


def _resolve_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryValidationError(f"{label} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise RegistryValidationError(f"{label} is not a directory: {resolved}")
    return resolved


def _require_contained(path: Path, container: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(container)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RegistryValidationError(
            f"{label} escapes the allowed directory: {path}"
        ) from exc
    return resolved


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(
            f"cannot read valid JSON from {label}: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise RegistryValidationError(f"{label} must contain a JSON object: {path}")
    return payload


def _validate_created_at(value: Any, manifest_path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(
            f"manifest created_at must be a non-empty timestamp string: {manifest_path}"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryValidationError(
            f"manifest created_at is not an ISO-8601 timestamp: {manifest_path}"
        ) from exc
    if parsed.tzinfo is None:
        raise RegistryValidationError(
            f"manifest created_at must include a timezone: {manifest_path}"
        )
    return value


def _entry_from_manifest(manifest_path: Path, root: Path) -> dict[str, str]:
    manifest = _load_json_object(manifest_path, label="manifest")
    run_dir = manifest_path.parent

    model_id = manifest.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise RegistryValidationError(
            f"manifest model_id must be a non-empty string: {manifest_path}"
        )
    if model_id != run_dir.name:
        raise RegistryValidationError(
            f"manifest model_id must match its run directory name: {manifest_path}"
        )

    status = manifest.get("status")
    if not isinstance(status, str) or status not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise RegistryValidationError(
            f"manifest status must be one of {allowed}: {manifest_path}"
        )

    return {
        "created_at": _validate_created_at(manifest.get("created_at"), manifest_path),
        "manifest_path": _relative_path(manifest_path, root),
        "model_id": model_id,
        "run_dir": _relative_path(run_dir, root),
        "status": status,
    }


def _validated_layout(root: Path) -> tuple[Path, Path, Path]:
    resolved_root = _resolve_directory(root, label="data root")
    if resolved_root == Path(resolved_root.anchor):
        raise RegistryValidationError(
            f"refusing to use a drive or filesystem root: {resolved_root}"
        )

    candidates = _resolve_directory(
        resolved_root / "models" / "rl" / "candidates",
        label="RL candidates directory",
    )
    try:
        candidates.relative_to(resolved_root)
    except ValueError as exc:
        raise RegistryValidationError(
            f"RL candidates directory escapes the data root: {candidates}"
        ) from exc

    registry = resolved_root / "models" / "rl" / "registry.json"
    registry_parent = _require_contained(
        registry.parent,
        resolved_root,
        label="RL registry parent",
    )
    if registry.exists() or registry.is_symlink():
        resolved_registry = _require_contained(
            registry,
            registry_parent,
            label="RL registry",
        )
        if not resolved_registry.is_file():
            raise RegistryValidationError(
                f"RL registry target is not a file: {registry}"
            )
    return resolved_root, candidates, registry


def build_registry(root: Path) -> dict[str, Any]:
    """Build and validate registry content without writing it."""
    resolved_root, candidates, _ = _validated_layout(root)
    manifests: list[Path] = []
    for discovered in candidates.rglob("manifest.json"):
        manifest_path = _require_contained(
            discovered,
            candidates,
            label="RL manifest",
        )
        if not manifest_path.is_file():
            raise RegistryValidationError(f"RL manifest is not a file: {manifest_path}")
        manifests.append(manifest_path)

    manifests.sort(key=lambda path: _relative_path(path, resolved_root))
    entries = [_entry_from_manifest(path, resolved_root) for path in manifests]

    seen_model_ids: set[str] = set()
    for entry in entries:
        model_id = entry["model_id"]
        if model_id in seen_model_ids:
            raise RegistryValidationError(f"duplicate RL model_id: {model_id}")
        seen_model_ids.add(model_id)

    return {"version": REGISTRY_VERSION, "entries": entries}


def registry_path(root: Path) -> Path:
    """Return the validated registry path for a data root."""
    _, _, path = _validated_layout(root)
    return path


def write_registry_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a registry using a temporary file in the same directory."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def rebuild_registry(root: Path) -> dict[str, Any]:
    """Validate all manifests and atomically rebuild the registry."""
    payload = build_registry(root)
    path = registry_path(root)
    write_registry_atomic(path, payload)
    return payload


def check_registry(root: Path) -> dict[str, Any]:
    """Validate that the registry can be rebuilt without writing it."""
    return build_registry(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="CandleMind data root (defaults to MARKET_DATA_DIR or the Windows G drive layout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate candidate manifests and paths without writing registry.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve_data_root(args.root)
        payload = check_registry(root) if args.check else rebuild_registry(root)
        action = "validated" if args.check else "rebuilt"
        print(f"RL registry {action}: {registry_path(root)} ({len(payload['entries'])} entries)")
        return 0
    except (OSError, RegistryValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
