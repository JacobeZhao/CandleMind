"""Read-only verification for normalized observed-funding releases."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any

import numpy as np
import pandas as pd

from backend.app.services.funding_contract import FUNDING_MAX_GAP_MS


MANIFEST_NAME = "manifest.json"
RELEASE_SCHEMA = "candlemind-derivatives-release-v1"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")


class FundingReleaseError(ValueError):
    """Raised when a funding release is incomplete or cannot be trusted."""


def canonical_derivatives_manifest_sha256(payload: Mapping[str, Any]) -> str:
    """Return the exact canonical hash used by derivatives release manifests."""

    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_observed_funding_release(release_dir: Path) -> dict[str, Any]:
    """Verify a completed funding release and every registered symbol file."""

    root = _resolve_release_root(release_dir)
    manifest_path = root / MANIFEST_NAME
    _reject_symlink_chain(root, manifest_path)
    if not manifest_path.is_file():
        raise FundingReleaseError("funding release requires manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FundingReleaseError("funding manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise FundingReleaseError("funding manifest must be an object")
    if manifest.get("manifest_sha256") != canonical_derivatives_manifest_sha256(manifest):
        raise FundingReleaseError("funding manifest self-hash failed")
    if (
        manifest.get("schema") != RELEASE_SCHEMA
        or manifest.get("status") != "completed"
        or manifest.get("complete_universe") is not True
        or manifest.get("datasets") != ["funding"]
        or manifest.get("errors") != []
    ):
        raise FundingReleaseError("funding release is incomplete")
    symbols = manifest.get("symbols")
    outputs = manifest.get("outputs")
    if (
        not isinstance(symbols, list)
        or any(not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol) for symbol in symbols)
        or symbols != sorted(set(symbols))
        or not isinstance(outputs, list)
        or any(not isinstance(item, Mapping) for item in outputs)
        or len(outputs) != len(symbols)
        or manifest.get("output_count") != len(outputs)
    ):
        raise FundingReleaseError("funding manifest universe is malformed")
    _validate_output_registry(symbols, outputs)
    for symbol in symbols:
        load_observed_funding_symbol(root, symbol, manifest=manifest)
    if any(path.is_symlink() for path in root.rglob("*")):
        raise FundingReleaseError("funding release contains a symbolic link")
    try:
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        expected = {MANIFEST_NAME, *(item["path"] for item in outputs)}
    except (KeyError, TypeError, ValueError) as exc:
        raise FundingReleaseError("funding output registry is malformed") from exc
    if actual != expected:
        raise FundingReleaseError("funding release contains unregistered files")
    return manifest


def load_observed_funding_symbol(
    release_dir: Path, symbol: str, *, manifest: Mapping[str, Any]
) -> tuple[pd.DataFrame, Mapping[str, Any]]:
    """Load one symbol after validating its registry entry and persisted data."""

    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise FundingReleaseError("funding symbol format is invalid")
    root = _resolve_release_root(release_dir)
    outputs = manifest.get("outputs", ())
    if not isinstance(outputs, (list, tuple)):
        raise FundingReleaseError("funding output registry is malformed")
    matches = [
        item
        for item in outputs
        if isinstance(item, Mapping) and item.get("symbol") == symbol
    ]
    if len(matches) != 1:
        raise FundingReleaseError(f"funding symbol is not uniquely registered: {symbol}")
    record = matches[0]
    expected_path = f"funding/{symbol}.parquet"
    if record.get("dataset") != "funding" or record.get("path") != expected_path:
        raise FundingReleaseError(f"funding output path is invalid: {symbol}")
    unresolved_path = root / expected_path
    _reject_symlink_chain(root, unresolved_path)
    try:
        path = unresolved_path.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FundingReleaseError(f"funding output escapes release root: {symbol}")
    try:
        evidence_valid = (
            path.is_file()
            and path.stat().st_size == record.get("bytes")
            and _sha256_file(path) == record.get("sha256")
        )
    except OSError as exc:
        raise FundingReleaseError(f"funding byte evidence failed: {symbol}") from exc
    if not evidence_valid:
        raise FundingReleaseError(f"funding byte evidence failed: {symbol}")
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:
        raise FundingReleaseError(f"funding parquet is unreadable: {symbol}") from exc
    if tuple(frame.columns) != ("symbol", "event_time", "available_at", "funding_rate"):
        raise FundingReleaseError(f"funding schema failed: {symbol}")
    if len(frame) != record.get("rows") or frame.empty:
        raise FundingReleaseError(f"funding row count failed: {symbol}")
    if frame["symbol"].ne(symbol).any():
        raise FundingReleaseError(f"funding symbol values failed: {symbol}")
    try:
        event = pd.to_datetime(frame["event_time"], unit="ms", utc=True, errors="raise")
        available = pd.to_datetime(frame["available_at"], unit="ms", utc=True, errors="raise")
        rates = pd.to_numeric(frame["funding_rate"], errors="raise")
        values_valid = (
            event.notna().all()
            and available.notna().all()
            and event.is_monotonic_increasing
            and not event.duplicated().any()
            and not (available < event).any()
            and np.isfinite(rates.to_numpy(dtype=float)).all()
            and not rates.abs().ge(1.0).any()
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise FundingReleaseError(f"funding values failed: {symbol}") from exc
    if not values_valid:
        raise FundingReleaseError(f"funding values failed: {symbol}")
    if len(event) > 1 and event.diff().iloc[1:].max() > pd.Timedelta(
        milliseconds=FUNDING_MAX_GAP_MS
    ):
        raise FundingReleaseError(f"funding coverage gap exceeds contract: {symbol}")
    try:
        recorded_start = pd.Timestamp(record["start_utc"])
        recorded_end = pd.Timestamp(record["end_utc"])
        if recorded_start.tzinfo is None or recorded_end.tzinfo is None:
            raise ValueError("funding coverage timestamps must include a timezone")
        recorded_start = recorded_start.tz_convert("UTC")
        recorded_end = recorded_end.tz_convert("UTC")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FundingReleaseError(f"funding coverage evidence failed: {symbol}") from exc
    if recorded_start != event.iloc[0] or recorded_end != event.iloc[-1]:
        raise FundingReleaseError(f"funding coverage evidence failed: {symbol}")
    return frame, record


def _validate_output_registry(
    symbols: list[str], outputs: list[Mapping[str, Any]]
) -> None:
    registered_symbols: list[str] = []
    registered_paths: list[str] = []
    for record in outputs:
        symbol = record.get("symbol")
        path_value = record.get("path")
        if (
            not isinstance(symbol, str)
            or not _SYMBOL_RE.fullmatch(symbol)
            or not isinstance(path_value, str)
            or "\\" in path_value
            or PurePosixPath(path_value).is_absolute()
            or any(part in ("", ".", "..") for part in PurePosixPath(path_value).parts)
            or path_value != PurePosixPath(path_value).as_posix()
            or path_value != f"funding/{symbol}.parquet"
            or record.get("dataset") != "funding"
            or not isinstance(record.get("bytes"), int)
            or isinstance(record.get("bytes"), bool)
            or record["bytes"] < 0
            or not isinstance(record.get("rows"), int)
            or isinstance(record.get("rows"), bool)
            or record["rows"] <= 0
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or not isinstance(record.get("start_utc"), str)
            or not isinstance(record.get("end_utc"), str)
        ):
            raise FundingReleaseError("funding output registry is malformed")
        registered_symbols.append(symbol)
        registered_paths.append(path_value)
    if (
        sorted(registered_symbols) != symbols
        or len(set(registered_symbols)) != len(registered_symbols)
        or len(set(registered_paths)) != len(registered_paths)
    ):
        raise FundingReleaseError("funding output registry is malformed")


def _reject_symlink_chain(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise FundingReleaseError("funding output path contains a symbolic link")


def _resolve_release_root(release_dir: Path) -> Path:
    unresolved = release_dir.expanduser().absolute()
    current = Path(unresolved.anchor)
    try:
        for part in unresolved.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise FundingReleaseError(
                    "funding release path contains a symbolic link"
                )
        root = unresolved.resolve(strict=True)
    except OSError as exc:
        raise FundingReleaseError("funding release directory is unavailable") from exc
    if not root.is_dir():
        raise FundingReleaseError("funding release directory is unavailable")
    return root


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "FundingReleaseError",
    "canonical_derivatives_manifest_sha256",
    "load_observed_funding_symbol",
    "verify_observed_funding_release",
]
