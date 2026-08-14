"""Build an immutable EMA data release from explicit Parquet source files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from backend.app.services.ema_data_release import build_ema_data_release


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--ohlc-parquet",
        required=True,
        action="append",
        type=Path,
        help="Explicit 5m OHLC Parquet path; repeat once per symbol.",
    )
    parser.add_argument(
        "--universe-snapshots",
        required=True,
        type=Path,
        help="Explicit point-in-time universe snapshot Parquet path.",
    )
    parser.add_argument(
        "--universe-manifest",
        required=True,
        type=Path,
        help="Immutable PIT universe release manifest that produced the snapshot.",
    )
    parser.add_argument(
        "--pit-readiness",
        required=True,
        type=Path,
        help="Immutable ready PIT audit covering the universe window and symbols.",
    )
    parser.add_argument(
        "--warmup-days",
        required=True,
        type=int,
        help="Required continuous OHLC history before every eligible decision.",
    )
    parser.add_argument(
        "--label-horizon-days",
        required=True,
        type=int,
        help="Required continuous OHLC coverage after every eligible decision.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_ema_data_release(
        release_id=args.release_id,
        output_root=args.output_root,
        ohlc_paths=args.ohlc_parquet,
        universe_snapshots_path=args.universe_snapshots,
        universe_manifest_path=args.universe_manifest,
        pit_readiness_path=args.pit_readiness,
        warmup_days=args.warmup_days,
        label_horizon_days=args.label_horizon_days,
    )
    print(
        json.dumps(
            {
                "release_id": manifest["release_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "release_digest": manifest["release_digest"],
                "source_tree_sha256": manifest["source_snapshot"][
                    "source_tree_sha256"
                ],
                "output": str((args.output_root / args.release_id).resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
