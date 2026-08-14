"""Build and verify immutable point-in-time data releases for EMA research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.app.services.pit_universe_contract import (
    EMA_UNIVERSE_SCHEMA,
    UNIVERSE_COLUMNS,
    ema_universe_content_hash,
)


RELEASE_SCHEMA = "candlemind-ema-data-release-v2"
MANIFEST_NAME = "manifest.json"
UNIVERSE_OUTPUT_NAME = "universe_snapshots.parquet"
OHLC_OUTPUT_DIR = "ohlcv"
SOURCE_OUTPUT_DIR = "source"
EVIDENCE_OUTPUT_DIR = "evidence"
PIT_UNIVERSE_MANIFEST_NAME = "pit_universe_manifest.json"
PIT_READINESS_AUDIT_NAME = "pit_readiness_audit.json"
BAR_INTERVAL = pd.Timedelta(minutes=5)
SOURCE_SNAPSHOT_RELATIVE_PATHS = (
    "backend/app/services/ema_data_release.py",
    "backend/scripts/data/build_ema_data_release.py",
    "backend/app/rl/ema_universe.py",
    "backend/app/services/point_in_time_universe.py",
    "backend/app/rl/ema_features_v2.py",
    "backend/app/rl/ema_lifecycle.py",
)

_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OHLC_COLUMNS = ("open_time", "open", "high", "low", "close")
class EmaDataReleaseError(ValueError):
    """Raised when an EMA data release cannot be built or trusted."""


def build_ema_data_release(
    *,
    release_id: str,
    output_root: Path,
    ohlc_paths: Sequence[Path],
    universe_snapshots_path: Path,
    universe_manifest_path: Path,
    pit_readiness_path: Path,
    warmup_days: int,
    label_horizon_days: int,
    code_revision: str | None = None,
) -> dict[str, Any]:
    """Validate explicit sources, copy them, and atomically publish a release."""

    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise EmaDataReleaseError("release_id must be a simple 1-128 character key")
    if not ohlc_paths:
        raise EmaDataReleaseError("at least one explicit OHLC Parquet path is required")
    coverage = _coverage_contract(
        warmup_days=warmup_days,
        label_horizon_days=label_horizon_days,
    )

    root = output_root.expanduser().resolve()
    destination = root / release_id
    if destination.exists():
        raise FileExistsError(f"EMA data release already exists: {destination}")

    sources = [
        _explicit_parquet_path(path, label=f"OHLC source {index}")
        for index, path in enumerate(ohlc_paths, start=1)
    ]
    universe_source = _explicit_parquet_path(
        universe_snapshots_path, label="PIT universe snapshot source"
    )
    universe_manifest_source = _explicit_json_path(
        universe_manifest_path, label="PIT universe release manifest"
    )
    readiness_source = _explicit_json_path(
        pit_readiness_path, label="PIT readiness audit"
    )
    resolved_sources = [*sources, universe_source]
    if len(set(resolved_sources)) != len(resolved_sources):
        raise EmaDataReleaseError("source Parquet paths must be distinct")
    output_names = [path.name for path in sources]
    if len(set(output_names)) != len(output_names):
        raise EmaDataReleaseError("OHLC source file names must be unique")

    ohlc_records: list[dict[str, Any]] = []
    symbols: set[str] = set()
    ohlc_windows: list[dict[str, str]] = []
    ohlc_windows_by_symbol: dict[str, dict[str, str]] = {}
    for source in sources:
        frame = _read_parquet(source)
        symbol, window = _validate_ohlc_frame(frame, source)
        if symbol in symbols:
            raise EmaDataReleaseError(f"duplicate OHLC symbol: {symbol}")
        symbols.add(symbol)
        ohlc_windows.append(window)
        ohlc_windows_by_symbol[symbol] = window
        ohlc_records.append(
            _input_record(source, frame, kind="ohlcv", symbol=symbol, window=window)
        )

    universe_frame = _read_parquet(universe_source)
    universe_window, universe_contracts = _validate_universe_frame(universe_frame)
    universe_content_sha256 = ema_universe_content_hash(universe_frame)
    missing_symbols = sorted(symbols - universe_contracts)
    if missing_symbols:
        raise EmaDataReleaseError(
            "PIT universe snapshots omit OHLC symbols: " + ", ".join(missing_symbols)
        )
    eligible_symbols = set(
        universe_frame.loc[universe_frame["eligible"].astype(bool), "symbol"].astype(str)
    )
    missing_eligible_symbols = sorted(eligible_symbols - symbols)
    if missing_eligible_symbols:
        raise EmaDataReleaseError(
            "eligible PIT universe symbols lack OHLC data: "
            + ", ".join(missing_eligible_symbols)
        )
    coverage["eligible_pair_count"] = _validate_eligible_coverage(
        universe_frame,
        ohlc_windows_by_symbol,
        coverage,
    )
    universe_record = _input_record(
        universe_source,
        universe_frame,
        kind="point_in_time_universe",
        window=universe_window,
    )
    pit_evidence = _validate_pit_source_evidence(
        universe_manifest_source=universe_manifest_source,
        readiness_source=readiness_source,
        universe_source=universe_source,
        universe_frame=universe_frame,
        symbols=symbols,
    )
    release_window = _enclosing_window(ohlc_windows)

    revision = code_revision or _code_revision()
    if not isinstance(revision, str) or _CODE_REVISION_RE.fullmatch(revision) is None:
        raise EmaDataReleaseError("code_revision must be a lowercase Git object id")
    source_records = _source_snapshot_records()
    source_tree_sha256 = _source_tree_sha256(source_records)

    root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"EMA data release already exists: {destination}")
    staging = root / f".{release_id}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        (staging / OHLC_OUTPUT_DIR).mkdir()
        (staging / EVIDENCE_OUTPUT_DIR).mkdir()
        outputs: list[dict[str, Any]] = []
        for source, input_record in zip(sources, ohlc_records, strict=True):
            relative = Path(OHLC_OUTPUT_DIR) / source.name
            outputs.append(
                _copy_verified(source, staging / relative, relative, input_record)
            )
        universe_relative = Path(UNIVERSE_OUTPUT_NAME)
        outputs.append(
            _copy_verified(
                universe_source,
                staging / universe_relative,
                universe_relative,
                universe_record,
            )
        )
        persisted_pit_evidence = _copy_pit_evidence(
            staging,
            pit_evidence=pit_evidence,
            universe_manifest_source=universe_manifest_source,
            readiness_source=readiness_source,
        )
        persisted_source_records = _copy_source_snapshot(source_records, staging)

        manifest: dict[str, Any] = {
            "schema": RELEASE_SCHEMA,
            "release_id": release_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "code_revision": revision,
            "window": release_window,
            "coverage": coverage,
            "universe": {
                "schema": EMA_UNIVERSE_SCHEMA,
                "content_sha256": universe_content_sha256,
                "interval_semantics": "[effective_from,effective_to)",
            },
            "pit_evidence": persisted_pit_evidence,
            "inputs": [*ohlc_records, universe_record],
            "outputs": outputs,
            "source_snapshot": {
                "hash_algorithm": "sha256-path-and-content-v1",
                "source_tree_sha256": source_tree_sha256,
                "files": persisted_source_records,
            },
        }
        manifest["release_digest"] = canonical_release_digest(manifest)
        manifest["manifest_sha256"] = canonical_manifest_sha256(manifest)
        _write_json_exclusive(staging / MANIFEST_NAME, manifest)
        verified = verify_ema_data_release(staging)

        if destination.exists():
            raise FileExistsError(f"EMA data release already exists: {destination}")
        os.rename(staging, destination)
        return verified
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_ema_data_release(release_dir: Path) -> dict[str, Any]:
    """Reload every published file and verify byte and semantic evidence."""

    root = release_dir.expanduser().resolve()
    manifest_path = root / MANIFEST_NAME
    if not root.is_dir() or not manifest_path.is_file():
        raise EmaDataReleaseError("release requires a directory with manifest.json")
    manifest = _read_json(manifest_path)
    persisted_hash = manifest.get("manifest_sha256")
    if not isinstance(persisted_hash, str) or persisted_hash != canonical_manifest_sha256(
        manifest
    ):
        raise EmaDataReleaseError("manifest self-hash verification failed")
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise EmaDataReleaseError("unsupported EMA data release schema")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise EmaDataReleaseError("manifest release_id is invalid")
    _utc_timestamp(manifest.get("created_at"), field="created_at")
    revision = manifest.get("code_revision")
    if not isinstance(revision, str) or _CODE_REVISION_RE.fullmatch(revision) is None:
        raise EmaDataReleaseError("manifest code_revision is invalid")
    release_window = _validate_window(manifest.get("window"), field="release window")
    coverage = _validate_coverage_manifest(manifest.get("coverage"))
    universe_binding = _validate_universe_binding(manifest.get("universe"))
    pit_evidence = _validate_pit_evidence_manifest(manifest.get("pit_evidence"))
    release_digest = manifest.get("release_digest")
    if (
        not isinstance(release_digest, str)
        or _SHA256_RE.fullmatch(release_digest) is None
        or release_digest != canonical_release_digest(manifest)
    ):
        raise EmaDataReleaseError("release_digest verification failed")
    source_paths = _verify_source_snapshot(root, manifest.get("source_snapshot"))

    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    if not isinstance(inputs, list) or not inputs:
        raise EmaDataReleaseError("manifest inputs must be a non-empty list")
    if not isinstance(outputs, list) or len(outputs) != len(inputs):
        raise EmaDataReleaseError("manifest outputs must map every input")
    input_by_id: dict[str, Mapping[str, Any]] = {}
    for record in inputs:
        _validate_evidence_record(record, input_record=True)
        source_id = record["source_id"]
        if source_id in input_by_id:
            raise EmaDataReleaseError("manifest contains duplicate input source_id")
        input_by_id[source_id] = record

    registered_paths: set[str] = set()
    ohlc_windows: list[dict[str, str]] = []
    ohlc_symbols: set[str] = set()
    output_source_ids: set[str] = set()
    universe_contracts: set[str] | None = None
    universe_frame: pd.DataFrame | None = None
    ohlc_windows_by_symbol: dict[str, dict[str, str]] = {}
    for record in outputs:
        _validate_evidence_record(record, input_record=False)
        relative = record["path"]
        if relative in registered_paths:
            raise EmaDataReleaseError("manifest contains duplicate output path")
        registered_paths.add(relative)
        source = input_by_id.get(record["source_id"])
        if source is None:
            raise EmaDataReleaseError("output references an unknown input source_id")
        if record["source_id"] in output_source_ids:
            raise EmaDataReleaseError("multiple outputs reference the same input")
        output_source_ids.add(record["source_id"])
        for key in (
            "kind",
            "symbol",
            "rows",
            "bytes",
            "sha256",
            "semantic_sha256",
            "semantic_hash_algorithm",
            "schema",
            "window",
        ):
            if record.get(key) != source.get(key):
                raise EmaDataReleaseError(f"output evidence differs from input: {relative}")

        path = _safe_release_path(root, relative)
        _verify_file(path, record)
        frame = _read_parquet(path)
        if len(frame) != record["rows"]:
            raise EmaDataReleaseError(f"output row count verification failed: {relative}")
        if parquet_schema(frame, path) != record["schema"]:
            raise EmaDataReleaseError(f"output schema verification failed: {relative}")
        if dataframe_semantic_sha256(frame) != record["semantic_sha256"]:
            raise EmaDataReleaseError(f"output semantic hash verification failed: {relative}")
        if record["kind"] == "ohlcv":
            symbol, actual_window = _validate_ohlc_frame(frame, path)
            if symbol != record.get("symbol") or symbol in ohlc_symbols:
                raise EmaDataReleaseError(f"output symbol verification failed: {relative}")
            ohlc_symbols.add(symbol)
            ohlc_windows.append(actual_window)
            ohlc_windows_by_symbol[symbol] = actual_window
        elif record["kind"] == "point_in_time_universe":
            if universe_contracts is not None:
                raise EmaDataReleaseError("release contains multiple universe outputs")
            actual_window, universe_contracts = _validate_universe_frame(frame)
            if ema_universe_content_hash(frame) != universe_binding["content_sha256"]:
                raise EmaDataReleaseError("universe content hash verification failed")
            universe_frame = frame
        else:
            raise EmaDataReleaseError(f"unsupported output kind: {record['kind']}")
        if actual_window != record["window"]:
            raise EmaDataReleaseError(f"output UTC window verification failed: {relative}")

    if output_source_ids != set(input_by_id):
        raise EmaDataReleaseError("not every input has exactly one output")
    if universe_contracts is None or ohlc_symbols - universe_contracts:
        raise EmaDataReleaseError("universe output does not cover every OHLC symbol")
    if universe_frame is None:
        raise EmaDataReleaseError("release has no PIT universe output")
    evidence_paths = _verify_pit_evidence(
        root,
        pit_evidence=pit_evidence,
        universe_frame=universe_frame,
        universe_output=next(
            item for item in outputs if item["kind"] == "point_in_time_universe"
        ),
        symbols=ohlc_symbols,
    )
    eligible_symbols = set(
        universe_frame.loc[universe_frame["eligible"].astype(bool), "symbol"].astype(str)
    )
    if eligible_symbols - ohlc_symbols:
        raise EmaDataReleaseError("eligible PIT universe symbol lacks OHLC output")
    eligible_pair_count = _validate_eligible_coverage(
        universe_frame,
        ohlc_windows_by_symbol,
        coverage,
    )
    if eligible_pair_count != coverage["eligible_pair_count"]:
        raise EmaDataReleaseError("coverage eligible_pair_count verification failed")
    if not ohlc_windows or _enclosing_window(ohlc_windows) != release_window:
        raise EmaDataReleaseError("release window does not enclose all OHLC outputs")
    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    if actual_files != {
        MANIFEST_NAME,
        *registered_paths,
        *source_paths,
        *evidence_paths,
    }:
        raise EmaDataReleaseError("release contains missing or unregistered files")
    return manifest


def dataframe_semantic_sha256(frame: pd.DataFrame) -> str:
    """Return an order-sensitive content hash independent of Parquet encoding."""

    header = {
        "columns": [str(column) for column in frame.columns],
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "rows": len(frame),
    }
    digest = hashlib.sha256(_canonical_json(header))
    if not frame.empty:
        row_hashes = pd.util.hash_pandas_object(
            frame, index=False, categorize=False
        ).to_numpy(dtype="uint64")
        digest.update(row_hashes.tobytes())
    return digest.hexdigest()


def canonical_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a manifest with its self-hash field excluded."""

    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def canonical_release_digest(manifest: Mapping[str, Any]) -> str:
    """Return a registry key based on release content, not source paths or IDs."""

    output_identity = []
    outputs = manifest.get("outputs")
    if isinstance(outputs, list):
        for record in outputs:
            if not isinstance(record, Mapping):
                continue
            output_identity.append(
                {
                    "kind": record.get("kind"),
                    "symbol": record.get("symbol"),
                    "source_id": record.get("source_id"),
                    "bytes": record.get("bytes"),
                    "sha256": record.get("sha256"),
                    "semantic_sha256": record.get("semantic_sha256"),
                    "schema_sha256": (
                        record.get("schema", {}).get("sha256")
                        if isinstance(record.get("schema"), Mapping)
                        else None
                    ),
                    "window": record.get("window"),
                }
            )
    output_identity.sort(key=lambda item: (str(item["kind"]), str(item["symbol"])))
    source_snapshot = manifest.get("source_snapshot")
    source_tree_sha256 = (
        source_snapshot.get("source_tree_sha256")
        if isinstance(source_snapshot, Mapping)
        else None
    )
    pit_evidence = manifest.get("pit_evidence")
    pit_identity = None
    if isinstance(pit_evidence, Mapping):
        universe_evidence = pit_evidence.get("pit_universe_manifest")
        readiness_evidence = pit_evidence.get("pit_readiness_audit")
        if isinstance(universe_evidence, Mapping) and isinstance(
            readiness_evidence, Mapping
        ):
            pit_identity = {
                "universe_file_sha256": universe_evidence.get("sha256"),
                "universe_manifest_sha256": universe_evidence.get(
                    "manifest_sha256"
                ),
                "universe_release_id": universe_evidence.get("release_id"),
                "readiness_file_sha256": readiness_evidence.get("sha256"),
                "readiness_report_sha256": readiness_evidence.get("report_sha256"),
            }
    identity = {
        "schema": manifest.get("schema"),
        "window": manifest.get("window"),
        "coverage": manifest.get("coverage"),
        "universe": manifest.get("universe"),
        "pit_evidence": pit_identity,
        "outputs": output_identity,
        "source_tree_sha256": source_tree_sha256,
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def parquet_schema(frame: pd.DataFrame, path: Path) -> dict[str, Any]:
    """Describe both logical pandas types and persisted Arrow fields."""

    arrow = pq.ParquetFile(path).schema_arrow
    fields = [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in arrow
    ]
    payload = {
        "columns": [str(column) for column in frame.columns],
        "pandas_dtypes": [str(dtype) for dtype in frame.dtypes],
        "arrow_fields": fields,
    }
    payload["sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return payload


def _validate_ohlc_frame(
    frame: pd.DataFrame, source: Path
) -> tuple[str, dict[str, str]]:
    missing = [column for column in _OHLC_COLUMNS if column not in frame]
    if missing:
        raise EmaDataReleaseError(f"OHLC source missing columns {missing}: {source}")
    if frame.empty:
        raise EmaDataReleaseError(f"OHLC source is empty: {source}")
    times = _utc_series(frame["open_time"], field=f"{source.name}.open_time")
    if times.duplicated().any():
        raise EmaDataReleaseError(f"duplicate OHLC open_time: {source}")
    if not times.is_monotonic_increasing:
        raise EmaDataReleaseError(f"OHLC open_time must be strictly ordered: {source}")
    if len(times) > 1 and not times.diff().iloc[1:].eq(BAR_INTERVAL).all():
        raise EmaDataReleaseError(f"OHLC source has a missing/non-5m bar: {source}")

    numeric = frame.loc[:, ["open", "high", "low", "close"]].apply(
        pd.to_numeric, errors="coerce"
    )
    values = numeric.to_numpy(dtype=float)
    invalid = (
        not np.isfinite(values).all()
        or (values <= 0).any()
        or (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
        or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()
    )
    if invalid:
        raise EmaDataReleaseError(f"OHLC source contains invalid prices: {source}")

    if "symbol" in frame:
        symbol_values = frame["symbol"].dropna().astype(str).unique()
        if len(symbol_values) != 1:
            raise EmaDataReleaseError(f"OHLC source must contain one symbol: {source}")
        symbol = symbol_values[0]
    else:
        symbol = re.sub(r"_5m$", "", source.stem, flags=re.IGNORECASE)
    if not re.fullmatch(r"[A-Z0-9]{2,32}", symbol):
        raise EmaDataReleaseError(f"cannot derive uppercase symbol from: {source.name}")
    return symbol, _window(times.iloc[0], times.iloc[-1] + BAR_INTERVAL)


def _validate_universe_frame(
    frame: pd.DataFrame,
) -> tuple[dict[str, str], set[str]]:
    try:
        # The canonical hash function first applies the authoritative universe
        # schema, causality, interval, rank, and source-ID validator.
        ema_universe_content_hash(frame)
    except (TypeError, ValueError) as exc:
        raise EmaDataReleaseError(f"invalid canonical PIT universe: {exc}") from exc
    symbols = frame["symbol"]
    if symbols.isna().any() or symbols.astype(str).str.fullmatch(r"[A-Z0-9]{2,32}").ne(True).any():
        raise EmaDataReleaseError("PIT universe symbol values are invalid")
    effective_from = _utc_series(frame["effective_from"], field="effective_from")
    effective_to = _utc_series(frame["effective_to"], field="effective_to")
    return _window(effective_from.min(), effective_to.max()), set(symbols.astype(str))


def _validate_universe_binding(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "content_sha256",
        "interval_semantics",
    }:
        raise EmaDataReleaseError("manifest universe binding is malformed")
    if value.get("schema") != EMA_UNIVERSE_SCHEMA:
        raise EmaDataReleaseError("manifest universe schema is invalid")
    digest = value.get("content_sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise EmaDataReleaseError("manifest universe content hash is invalid")
    if value.get("interval_semantics") != "[effective_from,effective_to)":
        raise EmaDataReleaseError("manifest universe interval semantics are invalid")
    return value


def _validate_pit_source_evidence(
    *,
    universe_manifest_source: Path,
    readiness_source: Path,
    universe_source: Path,
    universe_frame: pd.DataFrame,
    symbols: set[str],
) -> dict[str, dict[str, Any]]:
    universe_manifest = _read_json(universe_manifest_source)
    readiness = _read_json(readiness_source)
    declared_output = universe_manifest.get("output")
    declared_relative = (
        declared_output.get("path") if isinstance(declared_output, Mapping) else None
    )
    if not isinstance(declared_relative, str) or not declared_relative:
        raise EmaDataReleaseError("PIT universe output path is missing")
    declared_path = (universe_manifest_source.parent / declared_relative).resolve()
    if declared_path != universe_source:
        raise EmaDataReleaseError(
            "PIT universe manifest does not identify the supplied Parquet"
        )
    components = readiness.get("components")
    pit_component = (
        components.get("pit_universe") if isinstance(components, Mapping) else None
    )
    evidence_paths = (
        pit_component.get("evidence_paths")
        if isinstance(pit_component, Mapping)
        else None
    )
    if (
        not isinstance(evidence_paths, list)
        or len(evidence_paths) != 1
        or Path(str(evidence_paths[0])).expanduser().resolve()
        != universe_manifest_source
    ):
        raise EmaDataReleaseError(
            "PIT readiness does not identify the supplied universe manifest"
        )
    identity = _validate_pit_payloads(
        universe_manifest,
        readiness,
        universe_sha256=_sha256_file(universe_source),
        universe_bytes=universe_source.stat().st_size,
        universe_frame=universe_frame,
        symbols=symbols,
    )
    return {
        "pit_universe_manifest": {
            "path": f"{EVIDENCE_OUTPUT_DIR}/{PIT_UNIVERSE_MANIFEST_NAME}",
            "source_path": str(universe_manifest_source),
            "bytes": universe_manifest_source.stat().st_size,
            "sha256": _sha256_file(universe_manifest_source),
            "manifest_sha256": identity["universe_manifest_sha256"],
            "release_id": identity["universe_release_id"],
        },
        "pit_readiness_audit": {
            "path": f"{EVIDENCE_OUTPUT_DIR}/{PIT_READINESS_AUDIT_NAME}",
            "source_path": str(readiness_source),
            "bytes": readiness_source.stat().st_size,
            "sha256": _sha256_file(readiness_source),
            "report_sha256": identity["readiness_report_sha256"],
            "status": "ready",
        },
    }


def _validate_pit_payloads(
    universe_manifest: Mapping[str, Any],
    readiness: Mapping[str, Any],
    *,
    universe_sha256: str,
    universe_bytes: int,
    universe_frame: pd.DataFrame,
    symbols: set[str],
) -> dict[str, str]:
    if (
        universe_manifest.get("schema") != "candlemind-pit-universe-release-v1"
        or universe_manifest.get("status") != "completed"
    ):
        raise EmaDataReleaseError("PIT universe release manifest is not completed")
    manifest_sha256 = universe_manifest.get("manifest_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or _SHA256_RE.fullmatch(manifest_sha256) is None
        or manifest_sha256
        != _canonical_object_hash(universe_manifest, excluded="manifest_sha256")
    ):
        raise EmaDataReleaseError("PIT universe release manifest hash is invalid")
    release_id = universe_manifest.get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise EmaDataReleaseError("PIT universe release_id is invalid")
    manifest_symbols = universe_manifest.get("symbols")
    if (
        not isinstance(manifest_symbols, list)
        or len(manifest_symbols) != len(set(manifest_symbols))
        or set(manifest_symbols) != symbols
    ):
        raise EmaDataReleaseError("PIT universe manifest symbols differ from OHLC symbols")
    if not isinstance(universe_manifest.get("records"), list) or not universe_manifest["records"]:
        raise EmaDataReleaseError("PIT universe release records are missing")
    output = universe_manifest.get("output")
    if not isinstance(output, Mapping):
        raise EmaDataReleaseError("PIT universe release output evidence is missing")
    expected_output = {
        "bytes": universe_bytes,
        "sha256": universe_sha256,
        "content_sha256": ema_universe_content_hash(universe_frame),
        "rows": len(universe_frame),
    }
    if any(output.get(key) != value for key, value in expected_output.items()):
        raise EmaDataReleaseError("PIT universe Parquet does not match its release manifest")

    if (
        readiness.get("schema") != "candlemind-pit-readiness-audit-v1"
        or readiness.get("status") != "ready"
        or readiness.get("blockers") != []
        or readiness.get("historical_membership_inferred") is not False
    ):
        raise EmaDataReleaseError("PIT readiness audit is not ready")
    report_sha256 = readiness.get("report_sha256")
    if (
        not isinstance(report_sha256, str)
        or _SHA256_RE.fullmatch(report_sha256) is None
        or report_sha256
        != _canonical_object_hash(readiness, excluded="report_sha256")
    ):
        raise EmaDataReleaseError("PIT readiness audit hash is invalid")
    readiness_symbols = readiness.get("symbols")
    if (
        not isinstance(readiness_symbols, list)
        or len(readiness_symbols) != len(set(readiness_symbols))
        or set(readiness_symbols) != symbols
    ):
        raise EmaDataReleaseError("PIT readiness symbols differ from OHLC symbols")
    expected_window = {
        "start": universe_manifest.get("requested_start"),
        "through": universe_manifest.get("requested_through"),
    }
    if readiness.get("window") != expected_window:
        raise EmaDataReleaseError("PIT readiness and universe windows differ")
    components = readiness.get("components")
    if (
        not isinstance(components, Mapping)
        or "pit_universe" not in components
        or any(
            not isinstance(component, Mapping) or component.get("status") != "ready"
            for component in components.values()
        )
    ):
        raise EmaDataReleaseError("PIT readiness contains a non-ready component")
    pit_component = components["pit_universe"]
    details = pit_component.get("details")
    if (
        not isinstance(details, Mapping)
        or details.get("manifest_sha256") != manifest_sha256
        or details.get("release_id") != release_id
    ):
        raise EmaDataReleaseError("PIT readiness does not bind the universe release")
    return {
        "universe_manifest_sha256": manifest_sha256,
        "universe_release_id": release_id,
        "readiness_report_sha256": report_sha256,
    }


def _validate_pit_evidence_manifest(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {
        "pit_universe_manifest",
        "pit_readiness_audit",
    }:
        raise EmaDataReleaseError("manifest PIT evidence binding is malformed")
    expected_fields = {
        "pit_universe_manifest": {
            "path",
            "source_path",
            "bytes",
            "sha256",
            "manifest_sha256",
            "release_id",
        },
        "pit_readiness_audit": {
            "path",
            "source_path",
            "bytes",
            "sha256",
            "report_sha256",
            "status",
        },
    }
    expected_paths = {
        "pit_universe_manifest": f"{EVIDENCE_OUTPUT_DIR}/{PIT_UNIVERSE_MANIFEST_NAME}",
        "pit_readiness_audit": f"{EVIDENCE_OUTPUT_DIR}/{PIT_READINESS_AUDIT_NAME}",
    }
    normalized: dict[str, Mapping[str, Any]] = {}
    for name, fields in expected_fields.items():
        record = value.get(name)
        if not isinstance(record, Mapping) or set(record) != fields:
            raise EmaDataReleaseError(f"manifest {name} evidence is malformed")
        if (
            not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool)
            or record["bytes"] <= 0
            or any(
                not isinstance(record[field], str)
                or _SHA256_RE.fullmatch(record[field]) is None
                for field in fields & {"sha256", "manifest_sha256", "report_sha256"}
            )
        ):
            raise EmaDataReleaseError(f"manifest {name} hash evidence is invalid")
        source_path = Path(str(record["source_path"]))
        if not source_path.is_absolute():
            raise EmaDataReleaseError(f"manifest {name} source path is not absolute")
        relative = Path(str(record["path"]))
        if (
            relative.as_posix() != expected_paths[name]
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise EmaDataReleaseError(f"manifest {name} output path is invalid")
        normalized[name] = record
    if normalized["pit_readiness_audit"].get("status") != "ready":
        raise EmaDataReleaseError("manifest PIT readiness status is invalid")
    release_id = normalized["pit_universe_manifest"].get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise EmaDataReleaseError("manifest PIT universe release_id is invalid")
    return normalized


def _copy_pit_evidence(
    staging: Path,
    *,
    pit_evidence: Mapping[str, Mapping[str, Any]],
    universe_manifest_source: Path,
    readiness_source: Path,
) -> dict[str, dict[str, Any]]:
    sources = {
        "pit_universe_manifest": universe_manifest_source,
        "pit_readiness_audit": readiness_source,
    }
    persisted: dict[str, dict[str, Any]] = {}
    for name, source in sources.items():
        record = dict(pit_evidence[name])
        if source.stat().st_size != record["bytes"] or _sha256_file(source) != record["sha256"]:
            raise EmaDataReleaseError(f"{name} changed while publishing")
        destination = _safe_release_path(staging, record["path"])
        shutil.copyfile(source, destination)
        if destination.stat().st_size != record["bytes"] or _sha256_file(destination) != record["sha256"]:
            raise EmaDataReleaseError(f"copied {name} evidence differs from source")
        persisted[name] = record
    return persisted


def _verify_pit_evidence(
    root: Path,
    *,
    pit_evidence: Mapping[str, Mapping[str, Any]],
    universe_frame: pd.DataFrame,
    universe_output: Mapping[str, Any],
    symbols: set[str],
) -> set[str]:
    paths: set[str] = set()
    payloads: dict[str, dict[str, Any]] = {}
    for name, record in pit_evidence.items():
        relative = str(record["path"])
        if relative in paths:
            raise EmaDataReleaseError("PIT evidence output paths must be unique")
        path = _safe_release_path(root, relative)
        if (
            not path.is_file()
            or path.stat().st_size != record["bytes"]
            or _sha256_file(path) != record["sha256"]
        ):
            raise EmaDataReleaseError(f"PIT evidence byte hash failed: {name}")
        payloads[name] = _read_json(path)
        paths.add(relative)
    identity = _validate_pit_payloads(
        payloads["pit_universe_manifest"],
        payloads["pit_readiness_audit"],
        universe_sha256=str(universe_output["sha256"]),
        universe_bytes=int(universe_output["bytes"]),
        universe_frame=universe_frame,
        symbols=symbols,
    )
    if (
        identity["universe_manifest_sha256"]
        != pit_evidence["pit_universe_manifest"]["manifest_sha256"]
        or identity["universe_release_id"]
        != pit_evidence["pit_universe_manifest"]["release_id"]
        or identity["readiness_report_sha256"]
        != pit_evidence["pit_readiness_audit"]["report_sha256"]
    ):
        raise EmaDataReleaseError("PIT evidence identity binding failed")
    return paths


def _input_record(
    path: Path,
    frame: pd.DataFrame,
    *,
    kind: str,
    window: Mapping[str, str],
    symbol: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "rows": len(frame),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "semantic_hash_algorithm": "pandas-row-hash-v1",
        "semantic_sha256": dataframe_semantic_sha256(frame),
        "schema": parquet_schema(frame, path),
        "window": dict(window),
    }
    if symbol is not None:
        record["symbol"] = symbol
    record["source_id"] = _source_record_id(record)
    return record


def _source_record_id(record: Mapping[str, Any]) -> str:
    """Identify source content independently of its mutable filesystem path."""

    identity = {
        "kind": record.get("kind"),
        "symbol": record.get("symbol"),
        "sha256": record.get("sha256"),
        "semantic_sha256": record.get("semantic_sha256"),
        "schema_sha256": (
            record.get("schema", {}).get("sha256")
            if isinstance(record.get("schema"), Mapping)
            else None
        ),
        "window": record.get("window"),
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _is_content_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    digest = value.removeprefix("sha256:")
    return _SHA256_RE.fullmatch(digest) is not None


def _coverage_contract(
    *, warmup_days: int, label_horizon_days: int
) -> dict[str, Any]:
    for field, value in (
        ("warmup_days", warmup_days),
        ("label_horizon_days", label_horizon_days),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EmaDataReleaseError(f"{field} must be a positive integer")
    return {
        "warmup_days": warmup_days,
        "label_horizon_days": label_horizon_days,
        "bar_interval": "5min",
        "required_window_semantics": "[decision-warmup,decision+label_horizon)",
    }


def _validate_coverage_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "warmup_days",
        "label_horizon_days",
        "bar_interval",
        "required_window_semantics",
        "eligible_pair_count",
    }:
        raise EmaDataReleaseError("manifest coverage contract is malformed")
    normalized = _coverage_contract(
        warmup_days=value.get("warmup_days"),
        label_horizon_days=value.get("label_horizon_days"),
    )
    count = value.get("eligible_pair_count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise EmaDataReleaseError("coverage eligible_pair_count must be positive")
    normalized["eligible_pair_count"] = count
    if dict(value) != normalized:
        raise EmaDataReleaseError("manifest coverage contract is not canonical")
    return normalized


def _validate_eligible_coverage(
    universe_frame: pd.DataFrame,
    ohlc_windows_by_symbol: Mapping[str, Mapping[str, str]],
    coverage: Mapping[str, Any],
) -> int:
    eligible = universe_frame.loc[
        universe_frame["eligible"].astype(bool), ["decision_time", "symbol"]
    ].copy()
    if eligible.empty:
        raise EmaDataReleaseError("PIT universe has no eligible decision-symbol pairs")
    decisions = _utc_series(eligible["decision_time"], field="decision_time")
    warmup = pd.Timedelta(days=int(coverage["warmup_days"]))
    horizon = pd.Timedelta(days=int(coverage["label_horizon_days"]))
    for index, decision in decisions.items():
        symbol = str(eligible.at[index, "symbol"])
        window = ohlc_windows_by_symbol.get(symbol)
        if window is None:
            raise EmaDataReleaseError(
                f"eligible PIT universe symbol lacks OHLC data: {symbol}"
            )
        actual = _validate_window(window, field=f"{symbol} OHLC window")
        actual_start = pd.Timestamp(actual["start"])
        actual_end = pd.Timestamp(actual["end"])
        required_start = decision - warmup
        required_end = decision + horizon
        if actual_start > required_start:
            raise EmaDataReleaseError(
                f"OHLC warmup coverage is insufficient for {symbol} at "
                f"{decision.isoformat()}: requires {required_start.isoformat()}"
            )
        if actual_end < required_end:
            raise EmaDataReleaseError(
                f"OHLC label horizon coverage is insufficient for {symbol} at "
                f"{decision.isoformat()}: requires {required_end.isoformat()}"
            )
    return len(eligible)


def _source_snapshot_records() -> list[dict[str, Any]]:
    repository = _repository_root()
    records: list[dict[str, Any]] = []
    for relative in SOURCE_SNAPSHOT_RELATIVE_PATHS:
        source = (repository / relative).resolve()
        try:
            source.relative_to(repository)
        except ValueError as exc:
            raise EmaDataReleaseError("source snapshot path escapes repository") from exc
        if not source.is_file() or source.is_symlink():
            raise EmaDataReleaseError(f"required source snapshot file is missing: {relative}")
        records.append(
            {
                "repository_path": relative,
                "path": f"{SOURCE_OUTPUT_DIR}/{relative}",
                "bytes": source.stat().st_size,
                "sha256": _sha256_file(source),
                "_source_path": source,
            }
        )
    return records


def _source_tree_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    identity = [
        {
            "repository_path": record.get("repository_path"),
            "bytes": record.get("bytes"),
            "sha256": record.get("sha256"),
        }
        for record in records
    ]
    identity.sort(key=lambda item: str(item["repository_path"]))
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _copy_source_snapshot(
    records: Sequence[Mapping[str, Any]], staging: Path
) -> list[dict[str, Any]]:
    persisted: list[dict[str, Any]] = []
    for record in records:
        source = record.get("_source_path")
        if not isinstance(source, Path):
            raise EmaDataReleaseError("source snapshot record lacks its source path")
        destination = _safe_release_path(staging, str(record["path"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if (
            _sha256_file(source) != record["sha256"]
            or _sha256_file(destination) != record["sha256"]
        ):
            raise EmaDataReleaseError(
                f"source changed while publishing snapshot: {record['repository_path']}"
            )
        persisted.append({key: value for key, value in record.items() if key != "_source_path"})
    return persisted


def _verify_source_snapshot(root: Path, value: Any) -> set[str]:
    if not isinstance(value, Mapping) or set(value) != {
        "hash_algorithm",
        "source_tree_sha256",
        "files",
    }:
        raise EmaDataReleaseError("manifest source_snapshot is malformed")
    if value.get("hash_algorithm") != "sha256-path-and-content-v1":
        raise EmaDataReleaseError("unsupported source snapshot hash algorithm")
    tree_hash = value.get("source_tree_sha256")
    if not isinstance(tree_hash, str) or _SHA256_RE.fullmatch(tree_hash) is None:
        raise EmaDataReleaseError("source_tree_sha256 is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise EmaDataReleaseError("source snapshot files must be non-empty")
    paths: set[str] = set()
    repository_paths: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for record in files:
        if not isinstance(record, Mapping) or set(record) != {
            "repository_path",
            "path",
            "bytes",
            "sha256",
        }:
            raise EmaDataReleaseError("source snapshot file record is malformed")
        repository_path = record.get("repository_path")
        relative = record.get("path")
        size = record.get("bytes")
        digest = record.get("sha256")
        if repository_path not in SOURCE_SNAPSHOT_RELATIVE_PATHS:
            raise EmaDataReleaseError("source snapshot declares an unexpected module")
        expected_path = f"{SOURCE_OUTPUT_DIR}/{repository_path}"
        if relative != expected_path or relative in paths or repository_path in repository_paths:
            raise EmaDataReleaseError("source snapshot path is invalid or duplicated")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise EmaDataReleaseError("source snapshot byte count is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise EmaDataReleaseError("source snapshot SHA-256 is invalid")
        path = _safe_release_path(root, relative)
        if not path.is_file() or path.stat().st_size != size or _sha256_file(path) != digest:
            raise EmaDataReleaseError(
                f"source snapshot byte hash verification failed: {repository_path}"
            )
        paths.add(relative)
        repository_paths.add(repository_path)
        normalized.append(dict(record))
    if repository_paths != set(SOURCE_SNAPSHOT_RELATIVE_PATHS):
        raise EmaDataReleaseError("source snapshot does not cover all required modules")
    if _source_tree_sha256(normalized) != tree_hash:
        raise EmaDataReleaseError("source_tree_sha256 verification failed")
    return paths


def _copy_verified(
    source: Path,
    destination: Path,
    relative: Path,
    input_record: Mapping[str, Any],
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if _sha256_file(source) != input_record["sha256"]:
        raise EmaDataReleaseError(f"source changed while publishing: {source}")
    if _sha256_file(destination) != input_record["sha256"]:
        raise EmaDataReleaseError(f"copied output differs from source: {relative}")
    record = {key: value for key, value in input_record.items() if key != "path"}
    record["path"] = relative.as_posix()
    return record


def _validate_evidence_record(record: Any, *, input_record: bool) -> None:
    if not isinstance(record, Mapping):
        raise EmaDataReleaseError("manifest evidence record must be an object")
    required = {
        "source_id",
        "kind",
        "path",
        "rows",
        "bytes",
        "sha256",
        "semantic_hash_algorithm",
        "semantic_sha256",
        "schema",
        "window",
    }
    if not required.issubset(record):
        raise EmaDataReleaseError("manifest evidence record is incomplete")
    for key in ("source_id", "sha256", "semantic_sha256"):
        if not isinstance(record[key], str) or _SHA256_RE.fullmatch(record[key]) is None:
            raise EmaDataReleaseError(f"manifest evidence {key} is invalid")
    for key in ("rows", "bytes"):
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EmaDataReleaseError(f"manifest evidence {key} is invalid")
    if record["semantic_hash_algorithm"] != "pandas-row-hash-v1":
        raise EmaDataReleaseError("unsupported semantic hash algorithm")
    if record["kind"] not in {"ohlcv", "point_in_time_universe"}:
        raise EmaDataReleaseError("manifest evidence kind is invalid")
    if record["kind"] == "ohlcv":
        if not isinstance(record.get("symbol"), str) or not re.fullmatch(
            r"[A-Z0-9]{2,32}", record["symbol"]
        ):
            raise EmaDataReleaseError("manifest OHLC symbol is invalid")
    elif "symbol" in record:
        raise EmaDataReleaseError("universe evidence must not declare a symbol")
    if not isinstance(record["path"], str) or not record["path"]:
        raise EmaDataReleaseError("manifest evidence path is invalid")
    if input_record:
        source_path = Path(record["path"])
        if not source_path.is_absolute() or any(
            Path(part).stem.casefold() == "current" for part in source_path.parts
        ):
            raise EmaDataReleaseError("input evidence must name an explicit absolute source")
        expected_source_id = _source_record_id(record)
        if record["source_id"] != expected_source_id:
            raise EmaDataReleaseError("input source_id does not match its content")
    else:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise EmaDataReleaseError("output path must remain inside the release")
    _validate_window(record["window"], field="evidence window")
    schema = record["schema"]
    if not isinstance(schema, Mapping) or not _SHA256_RE.fullmatch(str(schema.get("sha256", ""))):
        raise EmaDataReleaseError("manifest evidence schema is invalid")
    unsigned_schema = dict(schema)
    schema_hash = unsigned_schema.pop("sha256")
    if hashlib.sha256(_canonical_json(unsigned_schema)).hexdigest() != schema_hash:
        raise EmaDataReleaseError("manifest evidence schema hash is invalid")


def _verify_file(path: Path, record: Mapping[str, Any]) -> None:
    if not path.is_file():
        raise EmaDataReleaseError(f"release output is missing: {path.name}")
    if path.stat().st_size != record["bytes"] or _sha256_file(path) != record["sha256"]:
        raise EmaDataReleaseError(f"output byte hash verification failed: {path.name}")


def _explicit_parquet_path(path: Path, *, label: str) -> Path:
    return _explicit_source_path(path, label=label, suffix=".parquet")


def _explicit_json_path(path: Path, *, label: str) -> Path:
    return _explicit_source_path(path, label=label, suffix=".json")


def _explicit_source_path(path: Path, *, label: str, suffix: str) -> Path:
    raw = path.expanduser().absolute()
    if any(Path(part).stem.casefold() == "current" for part in raw.parts):
        raise EmaDataReleaseError(f"{label} must not use a current pointer: {raw}")
    cursor = raw
    while cursor != cursor.parent:
        if cursor.exists() and cursor.is_symlink():
            raise EmaDataReleaseError(f"{label} must not use a symlink: {raw}")
        cursor = cursor.parent
    resolved = raw.resolve()
    if resolved.suffix.casefold() != suffix:
        raise EmaDataReleaseError(f"{label} must be an explicit {suffix} file: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} does not exist: {resolved}")
    return resolved


def _safe_release_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EmaDataReleaseError("output path escapes the release") from exc
    return path


def _read_parquet(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        raise EmaDataReleaseError(f"cannot read Parquet file: {path}") from exc


def _utc_series(values: pd.Series, *, field: str) -> pd.Series:
    try:
        if pd.api.types.is_integer_dtype(values.dtype):
            converted = pd.to_datetime(values, unit="ms", utc=True, errors="coerce")
        else:
            converted = pd.to_datetime(values, utc=True, errors="coerce")
    except (TypeError, ValueError) as exc:
        raise EmaDataReleaseError(f"{field} must contain UTC timestamps") from exc
    if converted.isna().any():
        raise EmaDataReleaseError(f"{field} contains an invalid or missing timestamp")
    return pd.Series(converted, index=values.index)


def _window(start: Any, end: Any) -> dict[str, str]:
    start_utc = _utc_timestamp(start, field="window start")
    end_utc = _utc_timestamp(end, field="window end")
    if end_utc <= start_utc:
        raise EmaDataReleaseError("UTC window end must be after start")
    return {
        "start": start_utc.isoformat(),
        "end": end_utc.isoformat(),
        "semantics": "[start,end)",
    }


def _enclosing_window(windows: Sequence[Mapping[str, str]]) -> dict[str, str]:
    if not windows:
        raise EmaDataReleaseError("at least one UTC window is required")
    normalized = [
        _validate_window(value, field="OHLC window") for value in windows
    ]
    starts = [pd.Timestamp(value["start"]) for value in normalized]
    ends = [pd.Timestamp(value["end"]) for value in normalized]
    return _window(min(starts), max(ends))


def _validate_window(value: Any, *, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"start", "end", "semantics"}:
        raise EmaDataReleaseError(f"{field} is malformed")
    if value.get("semantics") != "[start,end)":
        raise EmaDataReleaseError(f"{field} must use half-open semantics")
    normalized = _window(value.get("start"), value.get("end"))
    if dict(value) != normalized:
        raise EmaDataReleaseError(f"{field} is not canonical UTC")
    return normalized


def _utc_timestamp(value: Any, *, field: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise EmaDataReleaseError(f"{field} must be a valid UTC timestamp") from exc
    if timestamp.tzinfo is None:
        raise EmaDataReleaseError(f"{field} must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _code_revision() -> str:
    repository = _repository_root()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EmaDataReleaseError("cannot resolve the repository code revision") from exc
    return result.stdout.strip().lower()


def _repository_root() -> Path:
    repository = Path(__file__).resolve().parents[3]
    if not (repository / "backend").is_dir():
        raise EmaDataReleaseError("cannot resolve the repository root")
    return repository


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
    ).encode("ascii") + b"\n"
    with path.open("xb") as handle:
        handle.write(encoded)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EmaDataReleaseError(f"invalid JSON file: {path.name}") from exc
    if not isinstance(value, dict):
        raise EmaDataReleaseError(f"JSON file must contain an object: {path.name}")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_object_hash(value: Mapping[str, Any], *, excluded: str) -> str:
    unsigned = dict(value)
    unsigned.pop(excluded, None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "EmaDataReleaseError",
    "MANIFEST_NAME",
    "RELEASE_SCHEMA",
    "build_ema_data_release",
    "canonical_manifest_sha256",
    "canonical_release_digest",
    "dataframe_semantic_sha256",
    "verify_ema_data_release",
]
