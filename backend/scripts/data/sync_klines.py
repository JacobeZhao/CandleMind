"""Synchronize the canonical 30-symbol Binance Futures K-line universe."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.app.data_layout import validate_data_root
from backend.app.services.market_data_sync import (
    SUPPORTED_INTERVALS,
    DataIntegrityError,
    archive_specs,
    audit_frame,
    audit_to_dict,
    build_canonical_5m,
    download_symbol_archives,
    missing_daily_archive_specs,
    publish_staging_directory,
    publish_staged_files,
    resample_ohlcv,
    write_json_atomic,
    write_parquet_atomic,
)


SYMBOLS = (
    "AAVEUSDT", "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT",
    "AVAXUSDT", "BCHUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
    "DOTUSDT", "ETCUSDT", "ETHUSDT", "FILUSDT", "GALAUSDT",
    "INJUSDT", "LDOUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT",
    "OPUSDT", "RUNEUSDT", "SEIUSDT", "SOLUSDT", "SUIUSDT",
    "TIAUSDT", "TRXUSDT", "UNIUSDT", "XLMUSDT", "XRPUSDT",
)
DEFAULT_START_MONTH = date(2019, 9, 1)


def parse_month(value: str) -> date:
    parsed = datetime.strptime(value, "%Y-%m").date()
    return parsed.replace(day=1)


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
    parser.add_argument("--start-month", type=parse_month, default=DEFAULT_START_MONTH)
    parser.add_argument(
        "--through",
        type=parse_day,
        default=datetime.now(timezone.utc).date() - timedelta(days=1),
        help="Last UTC day requested from Binance Vision (default: yesterday)",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--symbol", action="append", choices=SYMBOLS)
    parser.add_argument("--keep-staging", action="store_true")
    return parser


def run_sync(
    *,
    root: Path,
    symbols: tuple[str, ...],
    start_month: date,
    through: date,
    workers: int,
    keep_staging: bool = False,
) -> dict:
    root = validate_data_root(root, require_writable=True)
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    run_id = started_wall.strftime("%Y%m%dT%H%M%SZ")
    raw_root = root / "raw" / "klines_archive"
    normalized = root / "normalized" / "ohlcv_parquet"
    staging = root / "normalized" / f".ohlcv_parquet.staging-{run_id}"
    manifest_path = root / "manifests" / f"klines_sync_{run_id}.json"
    progress_path = root / "manifests" / "klines_sync_progress.json"

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    payload = {
        "schema": "candlemind-klines-sync-v1",
        "run_id": run_id,
        "status": "running",
        "started_at": started_wall.isoformat(),
        "source": "Binance Vision USD-M futures 5m archives",
        "root": str(root),
        "symbols": list(symbols),
        "complete_universe": set(symbols) == set(SYMBOLS),
        "intervals": list(SUPPORTED_INTERVALS),
        "start_month": start_month.isoformat(),
        "requested_through": through.isoformat(),
        "downloads": {"downloaded": 0, "cached": 0, "unavailable": 0, "bytes": 0},
        "datasets": [],
        "errors": [],
    }
    write_json_atomic(progress_path, payload)

    try:
        for index, symbol in enumerate(symbols, start=1):
            symbol_started = time.perf_counter()
            print(f"[{index}/{len(symbols)}] {symbol}: downloading archives", flush=True)
            results = download_symbol_archives(
                archive_specs(symbol, start_month=start_month, through=through),
                raw_root,
                workers=workers,
            )
            available_paths = [Path(item.path) for item in results if item.path]
            for item in results:
                payload["downloads"][item.status] += 1
                if item.status == "downloaded":
                    payload["downloads"]["bytes"] += item.size
            if not available_paths:
                raise DataIntegrityError(f"no Binance Vision archives available for {symbol}")

            frame_5m = build_canonical_5m(
                available_paths,
                existing_path=normalized / f"{symbol}_5m.parquet",
                closed_before_ms=int(
                    datetime.combine(
                        through + timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ).timestamp()
                    * 1000
                ),
            )
            repair_specs = missing_daily_archive_specs(frame_5m, symbol)
            if repair_specs:
                print(
                    f"[{index}/{len(symbols)}] {symbol}: repairing "
                    f"{len(repair_specs)} missing UTC days",
                    flush=True,
                )
                repair_results = download_symbol_archives(
                    repair_specs,
                    raw_root,
                    workers=workers,
                )
                for item in repair_results:
                    payload["downloads"][item.status] += 1
                    if item.status == "downloaded":
                        payload["downloads"]["bytes"] += item.size
                repair_paths = [Path(item.path) for item in repair_results if item.path]
                available_paths = sorted(set(available_paths + repair_paths))
                frame_5m = build_canonical_5m(
                    available_paths,
                    existing_path=normalized / f"{symbol}_5m.parquet",
                    closed_before_ms=int(
                        datetime.combine(
                            through + timedelta(days=1),
                            datetime.min.time(),
                            tzinfo=timezone.utc,
                        ).timestamp()
                        * 1000
                    ),
                )
            symbol_datasets = []
            for interval in SUPPORTED_INTERVALS:
                frame = resample_ohlcv(frame_5m, interval)
                audit = audit_frame(frame, symbol, interval)
                output = staging / f"{symbol}_{interval}.parquet"
                byte_count = write_parquet_atomic(frame, output)
                symbol_datasets.append(audit_to_dict(audit, byte_count=byte_count))
            payload["datasets"].extend(symbol_datasets)
            payload["last_completed_symbol"] = symbol
            payload["last_symbol_seconds"] = time.perf_counter() - symbol_started
            write_json_atomic(progress_path, payload)
            print(
                f"[{index}/{len(symbols)}] {symbol}: "
                f"{len(frame_5m):,} 5m bars, {payload['last_symbol_seconds']:.1f}s",
                flush=True,
            )

        expected = len(symbols) * len(SUPPORTED_INTERVALS)
        files = list(staging.glob("*.parquet"))
        if len(files) != expected or len(payload["datasets"]) != expected:
            raise DataIntegrityError(
                f"expected {expected} staged datasets, found {len(files)} files and "
                f"{len(payload['datasets'])} audits"
            )
        if payload["complete_universe"]:
            publish_staging_directory(staging, normalized)
        else:
            publish_staged_files(staging, normalized)
        elapsed = time.perf_counter() - started
        payload.update(
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=elapsed,
            dataset_count=expected,
            total_rows=sum(item["rows"] for item in payload["datasets"]),
            total_parquet_bytes=sum(item["bytes"] for item in payload["datasets"]),
            archives_per_second=(
                sum(payload["downloads"][key] for key in ("downloaded", "cached", "unavailable"))
                / elapsed
            ),
        )
        write_json_atomic(manifest_path, payload)
        if payload["complete_universe"]:
            write_json_atomic(root / "manifests" / "klines_sync_current.json", payload)
        progress_path.unlink(missing_ok=True)
        return payload
    except Exception as exc:
        payload["status"] = "failed"
        payload["errors"].append(f"{type(exc).__name__}: {exc}")
        payload["failed_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(progress_path, payload)
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = tuple(dict.fromkeys(args.symbol)) if args.symbol else SYMBOLS
    result = run_sync(
        root=args.root,
        symbols=symbols,
        start_month=args.start_month,
        through=args.through,
        workers=args.workers,
        keep_staging=args.keep_staging,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "datasets"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
