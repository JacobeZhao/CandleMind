from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from backend.app.services.pit_universe_contract import ema_universe_content_hash


def write_pit_evidence(
    root: Path,
    universe_path: Path,
    *,
    release_id: str = "pit-universe-test-v1",
) -> tuple[Path, Path]:
    frame = pd.read_parquet(universe_path)
    symbols = sorted(frame["symbol"].astype(str).unique())
    source_sha256 = _file_sha256(universe_path)
    try:
        content_sha256 = ema_universe_content_hash(frame)
    except (TypeError, ValueError):
        # Invalid-universe tests must reach the production validator before
        # the synthetic lineage evidence is inspected.
        content_sha256 = "0" * 64
    suffix = source_sha256[:12]
    universe_manifest_path = root / f"pit_universe_{suffix}.json"
    readiness_path = root / f"pit_readiness_{suffix}.json"
    universe_manifest: dict[str, Any] = {
        "schema": "candlemind-pit-universe-release-v1",
        "status": "completed",
        "release_id": release_id,
        "requested_start": "2025-01-01",
        "requested_through": "2025-01-31",
        "symbols": symbols,
        "records": [{"source_id": "test"}],
        "output": {
            "path": universe_path.name,
            "bytes": universe_path.stat().st_size,
            "sha256": source_sha256,
            "content_sha256": content_sha256,
            "rows": len(frame),
        },
    }
    universe_manifest["manifest_sha256"] = _object_hash(
        universe_manifest, excluded="manifest_sha256"
    )
    readiness: dict[str, Any] = {
        "schema": "candlemind-pit-readiness-audit-v1",
        "status": "ready",
        "blockers": [],
        "historical_membership_inferred": False,
        "window": {"start": "2025-01-01", "through": "2025-01-31"},
        "symbols": symbols,
        "components": {
            "pit_universe": {
                "status": "ready",
                "evidence_paths": [str(universe_manifest_path.resolve())],
                "details": {
                    "manifest_sha256": universe_manifest["manifest_sha256"],
                    "release_id": release_id,
                },
            }
        },
    }
    readiness["report_sha256"] = _object_hash(
        readiness, excluded="report_sha256"
    )
    _write_json(universe_manifest_path, universe_manifest)
    _write_json(readiness_path, readiness)
    return universe_manifest_path, readiness_path


def _object_hash(value: Mapping[str, Any], *, excluded: str) -> str:
    unsigned = dict(value)
    unsigned.pop(excluded, None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
