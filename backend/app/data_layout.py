"""Validation and selection rules for CandleMind data roots."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REQUIRED_DIRECTORIES = (
    "raw",
    "raw/klines_archive",
    "raw/funding",
    "raw/derivatives_archive",
    "normalized",
    "normalized/ohlcv_parquet",
    "normalized/ema",
    "normalized/ema/releases",
    "normalized/derivatives",
    "normalized/derivatives/releases",
    "processed",
    "processed/features_app",
    "experiments",
    "experiments/backtests",
    "experiments/reports",
    "runtime",
    "runtime/app",
    "manifests",
)


class DataLayoutError(ValueError):
    """Raised when a path cannot safely serve as a CandleMind data root."""


@dataclass(frozen=True)
class DataRootSelection:
    root: Path
    authoritative: bool


def _is_filesystem_root(path: Path) -> bool:
    return path == Path(path.anchor)


def assert_writable_directory(path: Path) -> None:
    """Verify writability with a create/remove probe instead of ``os.access``."""
    probe: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".candlemind-write-probe-", dir=path)
        os.close(descriptor)
        probe = Path(name)
        probe.unlink()
    except OSError as exc:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        raise DataLayoutError(f"data root is not writable: {path}") from exc


def validate_data_root(root: Path, *, require_writable: bool = False) -> Path:
    """Validate the complete authoritative layout without creating directories."""
    try:
        resolved = root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DataLayoutError(f"data root does not exist: {root}") from exc
    if not resolved.is_dir():
        raise DataLayoutError(f"data root is not a directory: {resolved}")
    if _is_filesystem_root(resolved):
        raise DataLayoutError(f"refusing to use a drive or filesystem root: {resolved}")

    missing: list[str] = []
    for name in REQUIRED_DIRECTORIES:
        child = resolved / name
        if not child.is_dir():
            missing.append(name)
            continue
        try:
            child.resolve(strict=True).relative_to(resolved)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DataLayoutError(
                f"data root directory escapes the root: {child}"
            ) from exc
    if missing:
        raise DataLayoutError(
            "invalid CandleMind data layout; missing directories: "
            + ", ".join(missing)
        )
    if require_writable:
        assert_writable_directory(resolved)
    return resolved


def select_data_root(
    *,
    market_data_dir: str | None,
    data_dir: str | None,
    platform: str | None = None,
    default_windows_root: Path = Path("G:/CandleMind/CandleMind_data"),
    validator: Callable[..., Path] = validate_data_root,
) -> DataRootSelection:
    """Select the authoritative market-data root without runtime fallbacks."""
    if market_data_dir is not None:
        if not market_data_dir.strip():
            raise DataLayoutError("MARKET_DATA_DIR is set but empty")
        root = validator(Path(market_data_dir), require_writable=True)
        return DataRootSelection(root=root, authoritative=True)

    if (platform or os.name) == "nt":
        root = validator(default_windows_root, require_writable=True)
        return DataRootSelection(root=root, authoritative=True)

    runtime_hint = " DATA_DIR configures runtime state only." if data_dir else ""
    raise DataLayoutError(
        "MARKET_DATA_DIR is required outside Windows." + runtime_hint
    )
