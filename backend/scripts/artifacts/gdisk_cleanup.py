"""Quarantine explicitly allowed CandleMind artifacts without deleting them."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backend.app.data_layout import validate_data_root


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_SUBTREES = (
    ("models", "archive"),
    ("models", "rl", "candidates"),
    ("models", "rl", "walk_forward"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_cleanup_root(root: Path) -> Path:
    """Require the same complete, writable layout used by the application."""
    return validate_data_root(root, require_writable=True)


def _path_within_root(root: Path, path: Path, *, strict: bool) -> Path:
    try:
        resolved = path.resolve(strict=strict)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"path escapes cleanup root {root}: {path}") from exc
    return resolved


def _normalize_allowlist_entry(value: str | Path) -> Path:
    raw = str(value)
    relative = Path(raw)
    if not raw.strip() or relative.is_absolute() or relative.anchor:
        raise ValueError(f"cleanup allowlist entry must be relative: {value}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe cleanup allowlist entry: {value}")
    return relative


def _is_allowed_target(relative: Path) -> bool:
    parts = relative.parts
    for prefix in _ALLOWED_SUBTREES:
        if parts[: len(prefix)] == prefix and len(parts) > len(prefix):
            return True
    return (
        len(parts) == 3
        and parts[:2] == ("models", "rl")
        and parts[2].startswith("ppo_")
        and relative.suffix.lower() == ".zip"
    )


def _validated_source(root: Path, relative: Path) -> Path:
    if not _is_allowed_target(relative):
        raise ValueError(
            "cleanup target is outside the explicit artifact allowlist; "
            f"research reports and authoritative data are protected: {relative.as_posix()}"
        )
    source = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"cleanup target traverses a symlink: {relative.as_posix()}")
    _path_within_root(root, source, strict=True)
    return source


def _manifest_path(root: Path, run_id: str, requested: Path | None) -> Path:
    path = requested or (root / "manifests" / "cleanup" / f"{run_id}.json")
    if not path.is_absolute():
        path = root / path
    resolved = _path_within_root(root, path.expanduser(), strict=False)
    try:
        resolved.relative_to(root / "manifests")
    except ValueError as exc:
        raise ValueError("cleanup manifest must be stored under root/manifests") from exc
    if resolved.exists():
        raise FileExistsError(f"cleanup manifest already exists: {resolved}")
    return resolved


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_cleanup_plan(
    root: Path,
    *,
    allowlist: Sequence[str | Path],
    run_id: str,
    apply_changes: bool,
) -> dict:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid cleanup run id: {run_id}")

    normalized: dict[str, Path] = {}
    for entry in allowlist:
        relative = _normalize_allowlist_entry(entry)
        normalized[relative.as_posix()] = relative

    ordered = [normalized[key] for key in sorted(normalized)]
    for index, parent in enumerate(ordered):
        for child in ordered[index + 1 :]:
            try:
                child.relative_to(parent)
            except ValueError:
                continue
            raise ValueError(
                "cleanup allowlist entries must not overlap: "
                f"{parent.as_posix()} and {child.as_posix()}"
            )

    operations = []
    for relative_text, relative in sorted(normalized.items()):
        _validated_source(root, relative)
        destination = Path("quarantine") / run_id / relative
        destination_path = _path_within_root(root, root / destination, strict=False)
        if destination_path.exists():
            raise FileExistsError(f"quarantine destination already exists: {destination_path}")
        operations.append(
            {
                "source": relative_text,
                "destination": destination.as_posix(),
                "status": "planned",
                "started_at": None,
                "completed_at": None,
                "error": None,
            }
        )

    return {
        "schema": "candlemind-cleanup-plan-v1",
        "run_id": run_id,
        "root": str(root),
        "created_at": _utc_now(),
        "apply": apply_changes,
        "status": "planned",
        "operations": operations,
    }


def apply_cleanup(root: Path, plan: dict, manifest_path: Path) -> None:
    """Move each planned target, persisting intent and outcome around every move."""
    plan["status"] = "applying"
    plan["started_at"] = _utc_now()
    write_json_atomic(manifest_path, plan)

    try:
        for operation in plan["operations"]:
            relative = _normalize_allowlist_entry(operation["source"])
            source = _validated_source(root, relative)
            destination = _path_within_root(
                root, root / Path(operation["destination"]), strict=False
            )
            if destination.exists():
                raise FileExistsError(f"quarantine destination already exists: {destination}")

            operation["status"] = "moving"
            operation["started_at"] = _utc_now()
            write_json_atomic(manifest_path, plan)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            operation["status"] = "moved"
            operation["completed_at"] = _utc_now()
            write_json_atomic(manifest_path, plan)
    except Exception as exc:
        operation["status"] = "failed"
        operation["error"] = f"{type(exc).__name__}: {exc}"
        operation["completed_at"] = _utc_now()
        plan["status"] = "failed"
        plan["completed_at"] = _utc_now()
        write_json_atomic(manifest_path, plan)
        raise

    plan["status"] = "completed"
    plan["completed_at"] = _utc_now()
    write_json_atomic(manifest_path, plan)


def run_cleanup(
    root: Path,
    *,
    allowlist: Sequence[str | Path] = (),
    apply_changes: bool = False,
    manifest_path: Path | None = None,
    run_id: str | None = None,
) -> dict:
    root = validate_cleanup_root(root)
    selected_run_id = run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    output_manifest = _manifest_path(root, selected_run_id, manifest_path)
    plan = build_cleanup_plan(
        root,
        allowlist=allowlist,
        run_id=selected_run_id,
        apply_changes=apply_changes,
    )
    plan["manifest_path"] = str(output_manifest)

    # The complete intent exists before the first quarantine directory or move.
    write_json_atomic(output_manifest, plan)
    if apply_changes:
        apply_cleanup(root, plan, output_manifest)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="RELATIVE_PATH",
        help="Explicit artifact path to quarantine; repeat for multiple targets",
    )
    parser.add_argument("--apply", action="store_true", help="Move planned targets")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_cleanup(
        args.root,
        allowlist=args.allow,
        apply_changes=args.apply,
        manifest_path=args.manifest,
        run_id=args.run_id,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
