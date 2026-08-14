"""Verified Binance Vision derivatives archives and causal normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from backend.app.services.funding_contract import FUNDING_MAX_GAP_MS


VISION_BASE = "https://data.binance.vision/data/futures/um"
FIVE_MINUTES_MS = 300_000
_SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})(?:\s+.*)?$")
_KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
_METRIC_COLUMNS = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


class DerivativesSyncError(RuntimeError):
    """Base error for derivatives synchronization."""


class DerivativesDataIntegrityError(DerivativesSyncError):
    """Raised when source or normalized data violates its contract."""


@dataclass(frozen=True)
class SourceContract:
    key: str
    vision_name: str
    period_mode: str
    interval: str | None
    availability: str


SOURCE_CONTRACTS = {
    "metrics": SourceContract(
        "metrics", "metrics", "daily", None, "source_time_plus_5m"
    ),
    "mark_price": SourceContract(
        "mark_price", "markPriceKlines", "mixed", "5m", "bar_close"
    ),
    "index_price": SourceContract(
        "index_price", "indexPriceKlines", "mixed", "5m", "bar_close"
    ),
    "premium_index": SourceContract(
        "premium_index", "premiumIndexKlines", "mixed", "5m", "bar_close"
    ),
    "funding": SourceContract(
        "funding", "fundingRate", "monthly", None, "event_time"
    ),
    "book_depth": SourceContract(
        "book_depth", "bookDepth", "daily", None, "completed_5m_bucket"
    ),
}


@dataclass(frozen=True)
class DerivativeArchiveSpec:
    source: str
    symbol: str
    period: str
    key: str

    @property
    def contract(self) -> SourceContract:
        try:
            return SOURCE_CONTRACTS[self.source]
        except KeyError as exc:
            raise ValueError(f"unsupported derivatives source: {self.source}") from exc

    @property
    def filename(self) -> str:
        contract = self.contract
        if contract.interval:
            return f"{self.symbol}-{contract.interval}-{self.key}.zip"
        return f"{self.symbol}-{contract.vision_name}-{self.key}.zip"

    @property
    def relative_url(self) -> str:
        contract = self.contract
        parts = [self.period, contract.vision_name, self.symbol]
        if contract.interval:
            parts.append(contract.interval)
        parts.append(self.filename)
        return "/".join(parts)

    @property
    def url(self) -> str:
        return f"{VISION_BASE}/{self.relative_url}"


@dataclass(frozen=True)
class DownloadResult:
    spec: DerivativeArchiveSpec
    status: str
    path: str | None
    size: int
    sha256: str | None
    elapsed_seconds: float


def _next_month(value: date) -> date:
    return date(value.year + value.month // 12, value.month % 12 + 1, 1)


def source_archive_specs(
    source: str,
    symbol: str,
    *,
    start: date,
    through: date,
) -> list[DerivativeArchiveSpec]:
    """Build daily-only or monthly-with-daily-boundaries archive requests."""
    if source not in SOURCE_CONTRACTS:
        raise ValueError(f"unsupported derivatives source: {source}")
    if through < start:
        raise ValueError("through must not precede start")
    contract = SOURCE_CONTRACTS[source]
    specs: list[DerivativeArchiveSpec] = []
    if contract.period_mode == "monthly":
        cursor = start.replace(day=1)
        final = through.replace(day=1)
        while cursor <= final:
            specs.append(
                DerivativeArchiveSpec(source, symbol, "monthly", cursor.strftime("%Y-%m"))
            )
            cursor = _next_month(cursor)
        return specs
    cursor = start
    while cursor <= through:
        if contract.period_mode == "mixed":
            month_end = _next_month(cursor.replace(day=1)) - timedelta(days=1)
            if cursor.day == 1 and month_end <= through:
                specs.append(
                    DerivativeArchiveSpec(source, symbol, "monthly", cursor.strftime("%Y-%m"))
                )
                cursor = month_end + timedelta(days=1)
                continue
        specs.append(DerivativeArchiveSpec(source, symbol, "daily", cursor.isoformat()))
        cursor += timedelta(days=1)
    return specs


def archive_path(root: Path, spec: DerivativeArchiveSpec) -> Path:
    contract = spec.contract
    parts = [root, Path(contract.key), Path(spec.period), Path(spec.symbol)]
    if contract.interval:
        parts.append(Path(contract.interval))
    return Path(*parts) / spec.filename


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(raw: bytes) -> str:
    lines = raw.decode("utf-8-sig").strip().splitlines()
    if not lines:
        raise DerivativesDataIntegrityError("empty Binance checksum payload")
    match = _SHA256_RE.fullmatch(lines[0].strip())
    if not match:
        raise DerivativesDataIntegrityError(
            f"invalid Binance checksum payload: {lines[0].strip()!r}"
        )
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


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def download_archive(
    spec: DerivativeArchiveSpec,
    archive_root: Path,
    *,
    timeout: float = 60.0,
    retries: int = 4,
    opener_factory: Callable[[], object] = urllib.request.build_opener,
) -> DownloadResult:
    """Download an archive only after validating its official SHA-256 file."""
    started = time.perf_counter()
    destination = archive_path(archive_root, spec)
    checksum_path = destination.with_suffix(destination.suffix + ".CHECKSUM")
    if destination.is_file() and checksum_path.is_file():
        try:
            expected = parse_checksum(checksum_path.read_bytes())
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is None and sha256_file(destination) == expected:
                    return DownloadResult(
                        spec, "cached", str(destination), destination.stat().st_size,
                        expected, time.perf_counter() - started,
                    )
        except (OSError, zipfile.BadZipFile, DerivativesDataIntegrityError):
            pass

    checksum_raw = _fetch_bytes(
        f"{spec.url}.CHECKSUM",
        timeout=timeout,
        retries=retries,
        opener_factory=opener_factory,
    )
    if checksum_raw is None:
        return DownloadResult(
            spec, "unavailable", None, 0, None, time.perf_counter() - started
        )
    expected = parse_checksum(checksum_raw)
    if destination.is_file():
        try:
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is None and sha256_file(destination) == expected:
                    _write_bytes_atomic(checksum_path, checksum_raw)
                    return DownloadResult(
                        spec, "cached", str(destination), destination.stat().st_size,
                        expected, time.perf_counter() - started,
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
        raise DerivativesSyncError(f"archive disappeared after checksum: {spec.url}")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise DerivativesDataIntegrityError(
            f"checksum mismatch for {spec.filename}: expected {expected}, got {actual}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(payload)
        with zipfile.ZipFile(temporary) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise DerivativesDataIntegrityError(
                    f"corrupt member {corrupt} in {spec.filename}"
                )
        os.replace(temporary, destination)
        _write_bytes_atomic(checksum_path, checksum_raw)
    finally:
        temporary.unlink(missing_ok=True)
    return DownloadResult(
        spec, "downloaded", str(destination), destination.stat().st_size,
        actual, time.perf_counter() - started,
    )


def download_archives(
    specs: Iterable[DerivativeArchiveSpec],
    archive_root: Path,
    *,
    workers: int = 4,
) -> list[DownloadResult]:
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    specs = list(specs)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_archive, spec, archive_root): spec for spec in specs
        }
        results = [future.result() for future in as_completed(futures)]
    return sorted(results, key=lambda item: (item.spec.source, item.spec.period, item.spec.key))


def _read_single_csv(path: Path, *, header: int | None = 0) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise DerivativesDataIntegrityError(
                f"expected one CSV in {path}, found {len(members)}"
            )
        with archive.open(members[0]) as source:
            return pd.read_csv(source, header=header, low_memory=False)


def _epoch_ms(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    missing = numeric.isna()
    if missing.any():
        parsed = pd.to_datetime(values[missing], utc=True, errors="coerce")
        parsed_ms = pd.Series(
            parsed.astype("int64") // 1_000_000,
            index=values[missing].index,
            dtype="float64",
        )
        parsed_ms.loc[parsed.isna()] = np.nan
        numeric.loc[missing] = parsed_ms
    micros = numeric > 100_000_000_000_000
    numeric.loc[micros] = np.floor(numeric.loc[micros] / 1000)
    if numeric.isna().any():
        raise DerivativesDataIntegrityError("source contains invalid timestamps")
    return numeric.astype("int64")


def _window(start: date, through: date) -> tuple[int, int]:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(through + timedelta(days=1), tz="UTC").timestamp() * 1000)
    return start_ms, end_ms


def _regular_audit(
    frame: pd.DataFrame,
    *,
    time_column: str,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    label: str,
) -> dict:
    if frame.empty:
        raise DerivativesDataIntegrityError(f"{label} is empty")
    times = pd.to_numeric(frame[time_column], errors="coerce")
    if times.isna().any() or times.duplicated().any() or not times.is_monotonic_increasing:
        raise DerivativesDataIntegrityError(f"{label} timestamps are invalid")
    actual = times.astype("int64").to_numpy()
    expected = np.arange(start_ms, end_ms, step_ms, dtype=np.int64)
    missing = np.setdiff1d(expected, actual, assume_unique=True)
    outside = actual[(actual < start_ms) | (actual >= end_ms)]
    if missing.size or outside.size:
        raise DerivativesDataIntegrityError(
            f"{label} coverage mismatch: {missing.size} missing, {outside.size} outside"
        )
    return {
        "rows": int(len(frame)),
        "start_utc": pd.Timestamp(int(actual[0]), unit="ms", tz="UTC").isoformat(),
        "end_utc": pd.Timestamp(int(actual[-1]), unit="ms", tz="UTC").isoformat(),
        "duplicates": 0,
        "gap_events": int(np.sum(np.diff(actual) != step_ms)),
        "missing_intervals": int(missing.size),
    }


def _read_price(paths: Iterable[Path], label: str) -> pd.DataFrame:
    frames = []
    for path in sorted(paths):
        raw = _read_single_csv(path, header=None)
        if raw.empty:
            continue
        if not str(raw.iloc[0, 0]).strip().lstrip("-").isdigit():
            raw = raw.iloc[1:].reset_index(drop=True)
        if raw.shape[1] < len(_KLINE_COLUMNS):
            raise DerivativesDataIntegrityError(
                f"{label} archive has only {raw.shape[1]} columns: {path}"
            )
        raw = raw.iloc[:, : len(_KLINE_COLUMNS)]
        raw.columns = _KLINE_COLUMNS
        frame = raw.loc[:, ["open_time", "close_time", "close"]].copy()
        frame["open_time"] = _epoch_ms(frame["open_time"])
        frame["close_time"] = _epoch_ms(frame["close_time"])
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frames.append(frame.dropna())
    if not frames:
        raise DerivativesDataIntegrityError(f"no {label} source rows")
    result = pd.concat(frames, ignore_index=True).sort_values("open_time")
    if result["open_time"].duplicated().any():
        raise DerivativesDataIntegrityError(f"duplicate {label} source timestamps")
    return result.reset_index(drop=True)


def build_basis(
    mark_paths: Iterable[Path],
    index_paths: Iterable[Path],
    premium_paths: Iterable[Path],
    *,
    symbol: str,
    start: date,
    through: date,
) -> tuple[pd.DataFrame, dict]:
    """Build close-only mark/index basis from completed 5-minute bars."""
    start_ms, end_ms = _window(start, through)
    sources = {}
    for name, paths in (
        ("mark", mark_paths), ("index", index_paths), ("premium", premium_paths)
    ):
        source = _read_price(paths, name)
        source = source[(source["open_time"] >= start_ms) & (source["open_time"] < end_ms)]
        _regular_audit(
            source, time_column="open_time", start_ms=start_ms, end_ms=end_ms,
            step_ms=FIVE_MINUTES_MS, label=f"{symbol} {name}",
        )
        sources[name] = source.rename(
            columns={"close_time": f"{name}_close_time", "close": f"{name}_close"}
        )
    result = sources["mark"].merge(sources["index"], on="open_time", validate="one_to_one")
    result = result.merge(sources["premium"], on="open_time", validate="one_to_one")
    if len(result) != len(sources["mark"]):
        raise DerivativesDataIntegrityError(f"{symbol} basis sources are not exactly aligned")
    if (result["index_close"] <= 0).any() or (result["mark_close"] <= 0).any():
        raise DerivativesDataIntegrityError(f"{symbol} basis contains non-positive prices")
    close_columns = ["mark_close_time", "index_close_time", "premium_close_time"]
    result.insert(0, "symbol", symbol)
    result["event_time"] = result["open_time"].astype("int64")
    result["available_at"] = result[close_columns].max(axis=1).astype("int64") + 1
    result["basis_close"] = result["mark_close"] / result["index_close"] - 1.0
    result["premium_basis_error"] = result["premium_close"] - result["basis_close"]
    output = result.loc[:, [
        "symbol", "event_time", "available_at", "mark_close", "index_close",
        "premium_close", "basis_close", "premium_basis_error",
    ]].reset_index(drop=True)
    audit = _regular_audit(
        output, time_column="event_time", start_ms=start_ms, end_ms=end_ms,
        step_ms=FIVE_MINUTES_MS, label=f"{symbol} basis",
    )
    audit["availability"] = "max_source_close_time_plus_1ms"
    return output, audit


def build_open_interest(
    paths: Iterable[Path], *, symbol: str, start: date, through: date
) -> tuple[pd.DataFrame, dict]:
    frames = [_read_single_csv(path) for path in sorted(paths)]
    if not frames:
        raise DerivativesDataIntegrityError(f"no {symbol} metrics archives")
    frame = pd.concat(frames, ignore_index=True)
    required = {"create_time", "symbol", *_METRIC_COLUMNS}
    missing = required - set(frame.columns)
    if missing:
        raise DerivativesDataIntegrityError(f"metrics columns missing: {sorted(missing)}")
    if set(frame["symbol"].dropna().astype(str)) != {symbol}:
        raise DerivativesDataIntegrityError(f"metrics symbol mismatch for {symbol}")
    frame["event_time"] = _epoch_ms(frame["create_time"])
    for column in _METRIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    start_ms, end_ms = _window(start, through)
    frame = frame[(frame["event_time"] >= start_ms) & (frame["event_time"] < end_ms)]
    frame = frame.sort_values("event_time").reset_index(drop=True)
    if frame.loc[:, _METRIC_COLUMNS].isna().any().any():
        raise DerivativesDataIntegrityError(f"{symbol} metrics contain missing values")
    if not np.isfinite(frame.loc[:, _METRIC_COLUMNS].to_numpy(dtype=float)).all():
        raise DerivativesDataIntegrityError(f"{symbol} metrics contain non-finite values")
    if (frame[["sum_open_interest", "sum_open_interest_value"]] < 0).any().any():
        raise DerivativesDataIntegrityError(f"{symbol} metrics contain negative OI")
    frame.insert(0, "available_at", frame["event_time"].astype("int64") + FIVE_MINUTES_MS)
    output = frame.loc[:, ["symbol", "event_time", "available_at", *_METRIC_COLUMNS]]
    audit = _regular_audit(
        output, time_column="event_time", start_ms=start_ms, end_ms=end_ms,
        step_ms=FIVE_MINUTES_MS, label=f"{symbol} open interest",
    )
    audit["availability"] = "source_time_plus_5m"
    return output.reset_index(drop=True), audit


def build_funding(
    paths: Iterable[Path], *, symbol: str, start: date, through: date
) -> tuple[pd.DataFrame, dict]:
    frames = [_read_single_csv(path) for path in sorted(paths)]
    if not frames:
        raise DerivativesDataIntegrityError(f"no {symbol} funding archives")
    frame = pd.concat(frames, ignore_index=True)
    time_column = "calc_time" if "calc_time" in frame.columns else frame.columns[0]
    rate_column = "last_funding_rate" if "last_funding_rate" in frame.columns else frame.columns[-1]
    frame["event_time"] = _epoch_ms(frame[time_column])
    frame["funding_rate"] = pd.to_numeric(frame[rate_column], errors="coerce")
    start_ms, end_ms = _window(start, through)
    frame = frame[(frame["event_time"] >= start_ms) & (frame["event_time"] < end_ms)]
    frame = frame.sort_values("event_time").reset_index(drop=True)
    if frame.empty or frame["event_time"].isna().any() or frame["funding_rate"].isna().any():
        raise DerivativesDataIntegrityError(f"{symbol} funding is empty or invalid")
    if frame["event_time"].duplicated().any():
        raise DerivativesDataIntegrityError(f"{symbol} funding timestamps are duplicated")
    output = pd.DataFrame({
        "symbol": symbol,
        "event_time": frame["event_time"].astype("int64"),
        "available_at": frame["event_time"].astype("int64"),
        "funding_rate": frame["funding_rate"].astype(float),
    })
    times = output["event_time"].to_numpy(dtype=np.int64)
    gaps = np.diff(times)
    maximum_allowed_gap = FUNDING_MAX_GAP_MS
    if (
        times[0] > start_ms + maximum_allowed_gap
        or times[-1] < end_ms - maximum_allowed_gap
        or (gaps.size and gaps.max() > maximum_allowed_gap)
    ):
        raise DerivativesDataIntegrityError(
            f"{symbol} funding does not continuously cover the requested interval"
        )
    audit = {
        "rows": int(len(output)),
        "start_utc": pd.Timestamp(int(times[0]), unit="ms", tz="UTC").isoformat(),
        "end_utc": pd.Timestamp(int(times[-1]), unit="ms", tz="UTC").isoformat(),
        "duplicates": 0,
        "max_gap_hours": float(gaps.max() / 3_600_000) if gaps.size else 0.0,
        "availability": "actual_event_time",
    }
    return output, audit


def build_book_depth(
    paths: Iterable[Path], *, symbol: str, start: date, through: date
) -> tuple[pd.DataFrame, dict]:
    frames = [_read_single_csv(path) for path in sorted(paths)]
    if not frames:
        raise DerivativesDataIntegrityError(f"no {symbol} bookDepth archives")
    frame = pd.concat(frames, ignore_index=True)
    required = {"timestamp", "percentage", "depth", "notional"}
    missing = required - set(frame.columns)
    if missing:
        raise DerivativesDataIntegrityError(f"bookDepth columns missing: {sorted(missing)}")
    frame["snapshot_time"] = _epoch_ms(frame["timestamp"])
    frame["percentage"] = pd.to_numeric(frame["percentage"], errors="coerce")
    frame["depth"] = pd.to_numeric(frame["depth"], errors="coerce")
    frame["notional"] = pd.to_numeric(frame["notional"], errors="coerce")
    if frame[["snapshot_time", "percentage", "depth", "notional"]].isna().any().any():
        raise DerivativesDataIntegrityError(f"{symbol} bookDepth contains missing values")
    if (frame[["depth", "notional"]] < 0).any().any():
        raise DerivativesDataIntegrityError(f"{symbol} bookDepth contains negative depth")
    expected_bands = {-5, -4, -3, -2, -1, 1, 2, 3, 4, 5}
    band_counts = frame.groupby("snapshot_time")["percentage"].agg(
        lambda values: set(values.astype(int))
    )
    if not band_counts.map(lambda bands: bands == expected_bands).all():
        raise DerivativesDataIntegrityError(f"{symbol} bookDepth has incomplete bands")
    pivot = frame.pivot(index="snapshot_time", columns="percentage", values="notional")
    snapshots = pd.DataFrame(index=pivot.index)
    for band in (1, 5):
        bid = pivot[-band].astype(float)
        ask = pivot[band].astype(float)
        snapshots[f"bid_notional_{band}pct"] = bid
        snapshots[f"ask_notional_{band}pct"] = ask
        snapshots[f"depth_imbalance_{band}pct"] = (bid - ask) / (bid + ask).replace(0, np.nan)
    snapshots = snapshots.reset_index()
    start_ms, end_ms = _window(start, through)
    snapshots = snapshots[
        (snapshots["snapshot_time"] >= start_ms) & (snapshots["snapshot_time"] < end_ms)
    ].copy()
    snapshots["event_time"] = (
        snapshots["snapshot_time"].astype("int64") // FIVE_MINUTES_MS
    ) * FIVE_MINUTES_MS
    value_columns = [
        "bid_notional_1pct", "ask_notional_1pct", "depth_imbalance_1pct",
        "bid_notional_5pct", "ask_notional_5pct", "depth_imbalance_5pct",
    ]
    grouped = snapshots.groupby("event_time", sort=True)
    output = grouped[value_columns].median().reset_index()
    output["snapshot_count"] = grouped.size().to_numpy(dtype=np.int64)
    output.insert(0, "symbol", symbol)
    output.insert(2, "available_at", output["event_time"] + FIVE_MINUTES_MS)
    audit = _regular_audit(
        output, time_column="event_time", start_ms=start_ms, end_ms=end_ms,
        step_ms=FIVE_MINUTES_MS, label=f"{symbol} book depth",
    )
    audit.update(
        availability="completed_5m_bucket",
        minimum_snapshots_per_bar=int(output["snapshot_count"].min()),
        median_snapshots_per_bar=float(output["snapshot_count"].median()),
        source_semantics="aggregate_depth_at_1_to_5_percent_not_l2_or_spread",
    )
    return output.reset_index(drop=True), audit


def source_record(result: DownloadResult, *, root: Path) -> dict:
    payload = asdict(result)
    payload["spec"] = asdict(result.spec)
    if result.path:
        payload["path"] = str(Path(result.path).resolve().relative_to(root.resolve()))
    return payload


def canonical_manifest_sha256(payload: dict) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
