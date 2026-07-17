"""Create a checksummed source and metadata snapshot for an RL experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    entries = _source_entries()
    for report_path in args.report:
        entries[f"reports/{report_path.name}"] = report_path
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for index, fold in enumerate(report.get("folds", []), start=1):
            manifest_value = fold.get("train_result", {}).get("manifest_path")
            if not manifest_value:
                continue
            run_dir = Path(manifest_value).parent
            seed = _seed_label(Path(manifest_value))
            for name in ("manifest.json", "train_config.json", "feature_schema.json", "feature_scaler.json", "eval_summary.json"):
                path = run_dir / name
                if path.exists():
                    entries[f"model_metadata/{seed}/fold_{index:02d}/{name}"] = path

    payloads = {name: path.read_bytes() for name, path in sorted(entries.items())}
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_hash": _git_output(["git", "rev-parse", "HEAD"]),
        "git_status": _git_output(["git", "status", "--short"]),
        "files": {
            name: {"source": str(entries[name]), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in payloads.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
        archive.writestr("snapshot_manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({"output": str(args.output), "files": len(payloads)}, indent=2))


def _source_entries() -> dict[str, Path]:
    entries: dict[str, Path] = {}
    for path in sorted((ROOT / "backend" / "app" / "rl").glob("*.py")):
        entries[path.relative_to(ROOT).as_posix()] = path
    for path in sorted((ROOT / "backend" / "scripts").glob("*rl*.py")):
        entries[path.relative_to(ROOT).as_posix()] = path
    for path in sorted((ROOT / "backend" / "tests").glob("test_rl*.py")):
        entries[path.relative_to(ROOT).as_posix()] = path
    documentation = sorted((ROOT / "docs").glob("RL_*.md"))
    for path in [ROOT / "backend" / "requirements.txt", *documentation]:
        entries[path.relative_to(ROOT).as_posix()] = path
    return entries


def _seed_label(manifest_path: Path) -> str:
    for part in manifest_path.parts:
        if part.startswith("seed") and part[4:].isdigit():
            return part
    return "unknown_seed"


def _git_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


if __name__ == "__main__":
    main()
