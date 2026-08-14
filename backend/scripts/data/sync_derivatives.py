"""Publish a causal, versioned Binance Futures derivatives-data release."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.app.data_layout import validate_data_root
from backend.app.services.derivatives_data_sync import (
    SOURCE_CONTRACTS,
    DerivativesDataIntegrityError,
    build_basis,
    build_book_depth,
    build_funding,
    build_open_interest,
    canonical_manifest_sha256,
    download_archives,
    sha256_file,
    source_archive_specs,
    source_record,
)
from backend.app.services.market_data_sync import write_json_atomic, write_parquet_atomic
from backend.scripts.data.sync_klines import SYMBOLS


LOGICAL_DATASETS = ("open_interest", "basis", "funding", "book_depth")
DATASET_SOURCES = {
    "open_interest": ("metrics",),
    "basis": ("mark_price", "index_price", "premium_index"),
    "funding": ("funding",),
    "book_depth": ("book_depth",),
}
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("G:/CandleMind/CandleMind_data"),
        help="Validated CandleMind data root",
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--start", type=parse_day, required=True)
    parser.add_argument(
        "--through",
        type=parse_day,
        default=datetime.now(timezone.utc).date() - timedelta(days=1),
    )
    parser.add_argument("--symbol", action="append", choices=SYMBOLS)
    parser.add_argument("--dataset", action="append", choices=LOGICAL_DATASETS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--keep-staging", action="store_true")
    return parser


def _download_sources(
    *,
    root: Path,
    raw_root: Path,
    symbol: str,
    logical_dataset: str,
    start: date,
    through: date,
    workers: int,
) -> tuple[dict[str, list[Path]], list[dict]]:
    paths: dict[str, list[Path]] = {}
    records: list[dict] = []
    for source in DATASET_SOURCES[logical_dataset]:
        specs = source_archive_specs(source, symbol, start=start, through=through)
        results = download_archives(specs, raw_root, workers=workers)
        unavailable = [item.spec.url for item in results if item.status == "unavailable"]
        if unavailable:
            preview = ", ".join(unavailable[:3])
            raise DerivativesDataIntegrityError(
                f"{symbol} {source} is missing {len(unavailable)} requested archives: {preview}"
            )
        paths[source] = [Path(item.path) for item in results if item.path]
        records.extend(source_record(item, root=root) for item in results)
    return paths, records


def _build_dataset(
    logical_dataset: str,
    paths: dict[str, list[Path]],
    *,
    symbol: str,
    start: date,
    through: date,
):
    if logical_dataset == "open_interest":
        return build_open_interest(
            paths["metrics"], symbol=symbol, start=start, through=through
        )
    if logical_dataset == "basis":
        return build_basis(
            paths["mark_price"],
            paths["index_price"],
            paths["premium_index"],
            symbol=symbol,
            start=start,
            through=through,
        )
    if logical_dataset == "funding":
        return build_funding(
            paths["funding"], symbol=symbol, start=start, through=through
        )
    if logical_dataset == "book_depth":
        return build_book_depth(
            paths["book_depth"], symbol=symbol, start=start, through=through
        )
    raise ValueError(f"unsupported logical dataset: {logical_dataset}")


def run_sync(
    *,
    root: Path,
    release_id: str,
    symbols: tuple[str, ...],
    datasets: tuple[str, ...],
    start: date,
    through: date,
    workers: int,
    keep_staging: bool = False,
) -> dict:
    if not _RELEASE_ID_RE.fullmatch(release_id):
        raise ValueError("invalid release_id")
    if through < start:
        raise ValueError("through must not precede start")
    if not symbols or not datasets:
        raise ValueError("at least one symbol and dataset are required")
    if set(symbols) - set(SYMBOLS):
        raise ValueError("unsupported symbol")
    if set(datasets) - set(LOGICAL_DATASETS):
        raise ValueError("unsupported dataset")

    root = validate_data_root(root, require_writable=True)
    raw_root = root / "raw" / "derivatives_archive"
    releases_root = root / "normalized" / "derivatives" / "releases"
    release = releases_root / release_id
    staging = releases_root.parent / f".{release_id}.staging"
    manifest_path = root / "manifests" / f"derivatives_sync_{release_id}.json"
    progress_path = root / "manifests" / f"derivatives_sync_progress_{release_id}.json"
    if release.exists() or manifest_path.exists():
        raise FileExistsError(f"derivatives release already exists: {release_id}")
    resumed_from_interrupted_staging = staging.exists()
    if resumed_from_interrupted_staging:
        shutil.rmtree(staging)

    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    staging.mkdir(parents=True)
    payload = {
        "schema": "candlemind-derivatives-release-v1",
        "release_id": release_id,
        "status": "running",
        "started_at": started_wall.isoformat(),
        "source": "checksum-verified Binance Vision USD-M archives",
        "requested_start": start.isoformat(),
        "requested_through": through.isoformat(),
        "symbols": list(symbols),
        "datasets": list(datasets),
        "complete_universe": set(symbols) == set(SYMBOLS),
        "resumed_from_interrupted_staging": resumed_from_interrupted_staging,
        "source_contracts": {
            key: {
                "vision_name": value.vision_name,
                "period_mode": value.period_mode,
                "interval": value.interval,
                "availability": value.availability,
            }
            for key, value in SOURCE_CONTRACTS.items()
            if key in {source for dataset in datasets for source in DATASET_SOURCES[dataset]}
        },
        "source_archives": [],
        "outputs": [],
        "errors": [],
    }
    write_json_atomic(progress_path, payload)

    try:
        for symbol_index, symbol in enumerate(symbols, start=1):
            for dataset in datasets:
                print(
                    f"[{symbol_index}/{len(symbols)}] {symbol} {dataset}: syncing",
                    flush=True,
                )
                paths, records = _download_sources(
                    root=root,
                    raw_root=raw_root,
                    symbol=symbol,
                    logical_dataset=dataset,
                    start=start,
                    through=through,
                    workers=workers,
                )
                frame, audit = _build_dataset(
                    dataset, paths, symbol=symbol, start=start, through=through
                )
                relative = Path(dataset) / f"{symbol}.parquet"
                output = staging / relative
                byte_count = write_parquet_atomic(
                    frame, output, validation_column="event_time"
                )
                payload["source_archives"].extend(records)
                payload["outputs"].append({
                    "dataset": dataset,
                    "symbol": symbol,
                    "path": relative.as_posix(),
                    "bytes": byte_count,
                    "sha256": sha256_file(output),
                    **audit,
                })
                payload["last_completed"] = {"symbol": symbol, "dataset": dataset}
                write_json_atomic(progress_path, payload)

        payload.update(
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.perf_counter() - started,
            output_count=len(payload["outputs"]),
            total_rows=sum(item["rows"] for item in payload["outputs"]),
            total_output_bytes=sum(item["bytes"] for item in payload["outputs"]),
        )
        payload["manifest_sha256"] = canonical_manifest_sha256(payload)
        write_json_atomic(staging / "manifest.json", payload)
        os.replace(staging, release)
        write_json_atomic(manifest_path, payload)
        progress_path.unlink(missing_ok=True)
        return payload
    except Exception as exc:
        payload["status"] = "failed"
        payload["failed_at"] = datetime.now(timezone.utc).isoformat()
        payload["errors"].append(f"{type(exc).__name__}: {exc}")
        write_json_atomic(progress_path, payload)
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = tuple(dict.fromkeys(args.symbol)) if args.symbol else SYMBOLS
    datasets = tuple(dict.fromkeys(args.dataset)) if args.dataset else LOGICAL_DATASETS
    result = run_sync(
        root=args.root,
        release_id=args.release_id,
        symbols=symbols,
        datasets=datasets,
        start=args.start,
        through=args.through,
        workers=args.workers,
        keep_staging=args.keep_staging,
    )
    print(json.dumps({
        "release_id": result["release_id"],
        "status": result["status"],
        "manifest_sha256": result["manifest_sha256"],
        "output_count": result["output_count"],
        "total_rows": result["total_rows"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
