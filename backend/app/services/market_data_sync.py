"""Verified Binance Vision K-line synchronization and interval derivation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd


VISION_BASE = "https://data.binance.vision/data/futures/um"
CANONICAL_INTERVAL = "5m"
SUPPORTED_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "6h", "1d")
INTERVAL_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "1d": 86_400_000,
}
RESAMPLE_RULES = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "6h": "6h",
    "1d": "1D",
}
RAW_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)
STORE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "taker_buy_base",
)
NUMERIC_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base",
)
_SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})(?:\s+.*)?$")


class SyncError(RuntimeError):
    """Base synchronization failure."""


class DataIntegrityError(SyncError):
    """Raised when market data violates a correctness invariant."""


@dataclass(frozen=True)
class ArchiveSpec:
    symbol: str
    period: str
    key: str
    interval: str = CANONICAL_INTERVAL

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.key}.zip"

    @property
    def relative_url(self) -> str:
        return (
            f"{self.period}/klines/{self.symbol}/{self.interval}/{self.filename}"
        )

    @property
    def url(self) -> str:
        return f"{VISION_BASE}/{self.relative_url}"


@dataclass(frozen=True)
class DownloadResult:
    spec: ArchiveSpec
    status: str
    path: str | None
    size: int
    elapsed_seconds: float


@dataclass(frozen=True)
class DatasetAudit:
    symbol: str
    interval: str
    rows: int
    start_utc: str
    end_utc: str
    duplicates: int
    gap_events: int
    missing_bars: int
    bytes: int = 0


def month_floor(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    return date(value.year + value.month // 12, value.month % 12 + 1, 1)


def archive_specs(
    symbol: str,
    *,
    start_month: date,
    through: date,
) -> list[ArchiveSpec]:
    """Return completed monthly archives plus daily archives in the open month."""
    if start_month.day != 1:
        raise ValueError("start_month must be the first day of a month")
    if through < start_month:
        raise ValueError("through must not precede start_month")

    specs: list[ArchiveSpec] = []
    current_month = month_floor(through)
    cursor = start_month
    while cursor < current_month:
        specs.append(ArchiveSpec(symbol=symbol, period="monthly", key=cursor.strftime("%Y-%m")))
        cursor = next_month(cursor)

    cursor = current_month
    while cursor <= through:
        specs.append(ArchiveSpec(symbol=symbol, period="daily", key=cursor.isoformat()))
        cursor += timedelta(days=1)
    return specs


def archive_path(root: Path, spec: ArchiveSpec) -> Path:
    return (
        root
        / spec.period
        / spec.symbol
        / spec.interval
        / spec.filename
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(raw: bytes) -> str:
    first_line = raw.decode("utf-8-sig").strip().splitlines()[0].strip()
    match = _SHA256_RE.fullmatch(first_line)
    if not match:
        raise DataIntegrityError(f"invalid Binance checksum payload: {first_line!r}")
    return match.group(1).lower()


def _fetch_bytes(
    url: str,
    *,
    timeout: float,
    retries: int,
    opener_factory: Callable[[], object],
) -> bytes | None:
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            opener = opener_factory()
            request = urllib.request.Request(url, headers={"User-Agent": "CandleMind/1.0"})
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in {418, 429, 500, 502, 503, 504} or attempt >= retries:
                raise
        except (OSError, TimeoutError):
            if attempt >= retries:
                raise
        time.sleep(delay)
        delay = min(delay * 2.0, 30.0)
    raise AssertionError("unreachable retry loop")


def download_archive(
    spec: ArchiveSpec,
    archive_root: Path,
    *,
    timeout: float = 60.0,
    retries: int = 4,
    opener_factory: Callable[[], object] = urllib.request.build_opener,
) -> DownloadResult:
    """Download one immutable archive only after verifying its official checksum."""
    started = time.perf_counter()
    destination = archive_path(archive_root, spec)
    checksum_path = destination.with_suffix(destination.suffix + ".CHECKSUM")
    if destination.is_file() and checksum_path.is_file():
        try:
            expected = parse_checksum(checksum_path.read_bytes())
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is None and sha256_file(destination) == expected:
                    return DownloadResult(
                        spec,
                        "cached",
                        str(destination),
                        destination.stat().st_size,
                        time.perf_counter() - started,
                    )
        except (OSError, DataIntegrityError, zipfile.BadZipFile):
            pass

    checksum_raw = _fetch_bytes(
        f"{spec.url}.CHECKSUM",
        timeout=timeout,
        retries=retries,
        opener_factory=opener_factory,
    )
    if checksum_raw is None:
        return DownloadResult(spec, "unavailable", None, 0, time.perf_counter() - started)
    expected = parse_checksum(checksum_raw)

    if destination.is_file():
        try:
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is None and sha256_file(destination) == expected:
                    return DownloadResult(
                        spec,
                        "cached",
                        str(destination),
                        destination.stat().st_size,
                        time.perf_counter() - started,
                    )
        except (OSError, zipfile.BadZipFile):
            pass

    payload = _fetch_bytes(
        spec.url,
        timeout=timeout,
        retries=retries,
        opener_factory=opener_factory,
    )
    if payload is None:
        raise SyncError(f"archive disappeared after checksum was published: {spec.url}")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise DataIntegrityError(
            f"checksum mismatch for {spec.filename}: expected {expected}, got {actual}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(payload)
        with zipfile.ZipFile(temporary) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise DataIntegrityError(f"corrupt member {corrupt} in {spec.filename}")
        os.replace(temporary, destination)
        _write_bytes_atomic(checksum_path, checksum_raw)
    finally:
        temporary.unlink(missing_ok=True)
    return DownloadResult(
        spec,
        "downloaded",
        str(destination),
        destination.stat().st_size,
        time.perf_counter() - started,
    )


def download_symbol_archives(
    specs: Iterable[ArchiveSpec],
    archive_root: Path,
    *,
    workers: int = 4,
    timeout: float = 60.0,
    retries: int = 4,
) -> list[DownloadResult]:
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    specs = list(specs)
    results: list[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                download_archive,
                spec,
                archive_root,
                timeout=timeout,
                retries=retries,
            ): spec
            for spec in specs
        }
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: (item.spec.period, item.spec.key))


def _normalize_ms(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    micros = numeric > 100_000_000_000_000
    numeric.loc[micros] = numeric.loc[micros] // 1000
    return numeric


def read_archive(path: Path) -> pd.DataFrame:
    """Read one Binance ZIP while tolerating header and microsecond variants."""
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise DataIntegrityError(f"expected one CSV in {path}, found {len(members)}")
        with archive.open(members[0]) as source:
            raw = pd.read_csv(source, header=None, low_memory=False)
    if raw.empty:
        return pd.DataFrame(columns=STORE_COLUMNS)
    if not str(raw.iloc[0, 0]).strip().lstrip("-").isdigit():
        raw = raw.iloc[1:].reset_index(drop=True)
    if raw.shape[1] < len(RAW_COLUMNS):
        raise DataIntegrityError(f"archive has only {raw.shape[1]} columns: {path}")
    raw = raw.iloc[:, : len(RAW_COLUMNS)]
    raw.columns = RAW_COLUMNS
    frame = raw.loc[:, STORE_COLUMNS].copy()
    frame["open_time"] = _normalize_ms(frame["open_time"])
    frame["close_time"] = _normalize_ms(frame["close_time"])
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open_time", "close_time", *NUMERIC_COLUMNS])


def read_existing_5m(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame(columns=STORE_COLUMNS)
    frame = pd.read_parquet(path)
    missing = set(STORE_COLUMNS) - set(frame.columns)
    if missing:
        raise DataIntegrityError(f"existing 5m data misses columns: {sorted(missing)}")
    return frame.loc[:, STORE_COLUMNS].copy()


def build_canonical_5m(
    archive_paths: Iterable[Path],
    *,
    existing_path: Path | None = None,
    closed_before_ms: int | None = None,
) -> pd.DataFrame:
    frames = [read_archive(path) for path in sorted(archive_paths)]
    if existing_path is not None and existing_path.is_file():
        frames.append(read_existing_5m(existing_path))
    if not frames:
        raise DataIntegrityError("no available 5m archives or existing dataset")
    result = pd.concat(frames, ignore_index=True)
    result["open_time"] = _normalize_ms(result["open_time"])
    result["close_time"] = _normalize_ms(result["close_time"])
    if closed_before_ms is not None:
        result = result[result["close_time"] < closed_before_ms]
    result = (
        result.drop_duplicates("open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    return result.loc[:, STORE_COLUMNS]


def missing_daily_archive_specs(frame: pd.DataFrame, symbol: str) -> list[ArchiveSpec]:
    """Translate every 5m continuity gap into official daily repair archives."""
    if frame.empty:
        return []
    times = pd.to_numeric(frame["open_time"], errors="coerce").dropna().astype("int64")
    times = times.sort_values().drop_duplicates().to_numpy()
    step = INTERVAL_MS[CANONICAL_INTERVAL]
    days: set[date] = set()
    for left, right in zip(times[:-1], times[1:]):
        if right - left <= step:
            continue
        cursor = pd.Timestamp(int(left + step), unit="ms", tz="UTC").date()
        final = pd.Timestamp(int(right - step), unit="ms", tz="UTC").date()
        while cursor <= final:
            days.add(cursor)
            cursor += timedelta(days=1)
    return [
        ArchiveSpec(symbol=symbol, period="daily", key=day.isoformat())
        for day in sorted(days)
    ]


def resample_ohlcv(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval == CANONICAL_INTERVAL:
        return frame.copy()
    if interval not in RESAMPLE_RULES:
        raise ValueError(f"unsupported interval: {interval}")
    expected = INTERVAL_MS[interval] // INTERVAL_MS[CANONICAL_INTERVAL]
    indexed = frame.copy()
    indexed.index = pd.to_datetime(indexed["open_time"], unit="ms", utc=True)
    rule = RESAMPLE_RULES[interval]
    grouped = indexed.resample(rule, origin="epoch", label="left", closed="left")
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        close_time=("close_time", "last"),
        quote_volume=("quote_volume", "sum"),
        taker_buy_base=("taker_buy_base", "sum"),
    )
    counts = grouped["open_time"].count()
    result = result[counts == expected].copy()
    result.insert(0, "open_time", result.index.astype("int64") // 1_000_000)
    return result.loc[:, STORE_COLUMNS].reset_index(drop=True)


def audit_frame(frame: pd.DataFrame, symbol: str, interval: str) -> DatasetAudit:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    if frame.empty:
        raise DataIntegrityError(f"{symbol}_{interval} is empty")
    missing_columns = set(STORE_COLUMNS) - set(frame.columns)
    if missing_columns:
        raise DataIntegrityError(
            f"{symbol}_{interval} misses columns: {sorted(missing_columns)}"
        )
    times = pd.to_numeric(frame["open_time"], errors="coerce")
    if times.isna().any() or not times.is_monotonic_increasing:
        raise DataIntegrityError(f"{symbol}_{interval} timestamps are not increasing")
    duplicates = int(times.duplicated().sum())
    if duplicates:
        raise DataIntegrityError(f"{symbol}_{interval} has {duplicates} duplicates")
    step = INTERVAL_MS[interval]
    differences = times.diff().dropna()
    gap_events = int((differences > step).sum())
    missing_bars = int(((differences[differences > step] // step) - 1).sum())
    if gap_events or (differences % step != 0).any():
        raise DataIntegrityError(
            f"{symbol}_{interval} has {gap_events} gaps totaling {missing_bars} bars"
        )

    numeric = frame.loc[:, NUMERIC_COLUMNS].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise DataIntegrityError(f"{symbol}_{interval} contains non-finite OHLCV")
    if (
        (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
        or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()
        or (numeric[["open", "high", "low", "close"]] <= 0).any(axis=None)
        or (numeric[["volume", "quote_volume", "taker_buy_base"]] < 0).any(axis=None)
    ):
        raise DataIntegrityError(f"{symbol}_{interval} contains invalid OHLCV values")
    close_times = pd.to_numeric(frame["close_time"], errors="coerce")
    if close_times.isna().any() or (close_times < times).any():
        raise DataIntegrityError(f"{symbol}_{interval} has invalid close times")
    return DatasetAudit(
        symbol=symbol,
        interval=interval,
        rows=len(frame),
        start_utc=pd.Timestamp(int(times.iloc[0]), unit="ms", tz="UTC").isoformat(),
        end_utc=pd.Timestamp(int(times.iloc[-1]), unit="ms", tz="UTC").isoformat(),
        duplicates=duplicates,
        gap_events=gap_events,
        missing_bars=missing_bars,
    )


def write_parquet_atomic(
    frame: pd.DataFrame, path: Path, *, validation_column: str = "open_time"
) -> int:
    if validation_column not in frame.columns:
        raise DataIntegrityError(
            f"validation column {validation_column!r} is missing while writing {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False)
        persisted = pd.read_parquet(temporary, columns=[validation_column])
        if len(persisted) != len(frame):
            raise DataIntegrityError(f"row count changed while writing {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path.stat().st_size


def publish_staging_directory(staging: Path, destination: Path) -> None:
    """Publish a complete directory and restore the old one on any swap failure."""
    if not staging.is_dir():
        raise FileNotFoundError(f"staging directory does not exist: {staging}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(staging, destination)
    except Exception:
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def publish_staged_files(staging: Path, destination: Path) -> None:
    """Publish a validated partial refresh without removing unrelated datasets."""
    files = sorted(staging.glob("*.parquet"))
    if not files:
        raise DataIntegrityError(f"partial staging directory is empty: {staging}")
    destination.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".{destination.name}.partial-{uuid.uuid4().hex}"
    backup.mkdir()
    published: list[Path] = []
    try:
        for source in files:
            target = destination / source.name
            if target.exists():
                os.replace(target, backup / target.name)
            os.replace(source, target)
            published.append(target)
    except Exception:
        for target in published:
            target.unlink(missing_ok=True)
        for previous in backup.iterdir():
            os.replace(previous, destination / previous.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(staging)


def write_json_atomic(path: Path, payload: dict) -> None:
    _write_bytes_atomic(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def audit_to_dict(audit: DatasetAudit, *, byte_count: int) -> dict:
    payload = asdict(audit)
    payload["bytes"] = byte_count
    return payload
