"""Mark candidates referenced by walk-forward reports as rejected."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    updated = []
    for report_path in args.reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for fold in report.get("folds", []):
            manifest_value = fold.get("train_result", {}).get("manifest_path")
            if not manifest_value:
                continue
            manifest_path = Path(manifest_value)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "status": "rejected",
                "rejected_at": datetime.now(timezone.utc).isoformat(),
                "rejection_reason": args.reason,
                "rejection_report": str(report_path),
            })
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            updated.append(str(manifest_path))
    print(json.dumps({"updated": len(updated), "manifests": updated}, indent=2))


if __name__ == "__main__":
    main()
