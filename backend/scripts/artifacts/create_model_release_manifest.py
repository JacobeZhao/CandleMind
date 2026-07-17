"""Create an immutable artifact manifest for a supervised model release."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.scripts.artifacts.inventory_data_root import sha256_file


def build_release_manifest(
    release_dir: Path,
    *,
    output: Path,
    data_inventory_sha256: str,
    training_code_revision: str | None = None,
    training_data_snapshot: str | None = None,
    cost_assumptions: str | None = None,
) -> dict:
    release_dir = release_dir.expanduser().resolve(strict=True)
    output = output.expanduser().resolve(strict=False)
    if not release_dir.is_dir():
        raise NotADirectoryError(release_dir)
    try:
        output.relative_to(release_dir)
    except ValueError as exc:
        raise ValueError("release manifest must be written inside the release directory") from exc

    artifacts = []
    for path in sorted(release_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.resolve() == output:
            continue
        stat = path.stat()
        artifacts.append(
            {
                "name": path.name,
                "bytes": stat.st_size,
                "sha256": sha256_file(path),
            }
        )
    if not artifacts:
        raise ValueError(f"release contains no artifacts: {release_dir}")

    lineage_complete = all(
        (training_code_revision, training_data_snapshot, cost_assumptions)
    )
    return {
        "schema": "candlemind-model-release-v1",
        "release_id": release_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "documented" if lineage_complete else "legacy_incomplete",
        "immutable": True,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(item["bytes"] for item in artifacts),
        "data_inventory_sha256": data_inventory_sha256.lower(),
        "lineage": {
            "training_code_revision": training_code_revision,
            "training_data_snapshot": training_data_snapshot,
            "cost_assumptions": cost_assumptions,
            "complete": lineage_complete,
            "note": None if lineage_complete else (
                "Historical artifacts predate release manifests; unknown lineage "
                "fields are intentionally null and must not be inferred."
            ),
        },
        "artifacts": artifacts,
    }


def write_manifest_atomic(output: Path, payload: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise FileExistsError(f"release manifest is already sealed: {output}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-inventory-sha256", required=True)
    parser.add_argument("--training-code-revision")
    parser.add_argument("--training-data-snapshot")
    parser.add_argument("--cost-assumptions")
    args = parser.parse_args()
    from backend.app.datastore import validate_supervised_candidate_dir

    release_dir = validate_supervised_candidate_dir(args.release_dir)
    output = args.output or (release_dir / "release_manifest.json")
    payload = build_release_manifest(
        release_dir,
        output=output,
        data_inventory_sha256=args.data_inventory_sha256,
        training_code_revision=args.training_code_revision,
        training_data_snapshot=args.training_data_snapshot,
        cost_assumptions=args.cost_assumptions,
    )
    write_manifest_atomic(output, payload)
    print(f"Release manifest written: {output} ({payload['artifact_count']} artifacts)")


if __name__ == "__main__":
    main()
