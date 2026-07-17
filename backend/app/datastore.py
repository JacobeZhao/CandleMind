"""Central market-data paths with fail-closed authoritative root selection."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .data_layout import DataRootSelection, select_data_root


_DEFAULT_CLEAN = Path("G:/CandleMind/CandleMind_data")
_RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _configured_roots(configured: str | None, platform: str | None = None) -> list[Path]:
    """Return roots eligible for selection without legacy-path fallback."""
    if configured is not None:
        return [Path(configured)]
    if (platform or os.name) == "nt":
        return [_DEFAULT_CLEAN]
    return []


def _resolve_root(
    *,
    configured: str | None = None,
    data_dir: str | None = None,
    platform: str | None = None,
    default_windows_root: Path = _DEFAULT_CLEAN,
) -> DataRootSelection:
    return select_data_root(
        market_data_dir=(
            os.environ.get("MARKET_DATA_DIR") if configured is None else configured
        ),
        data_dir=os.environ.get("DATA_DIR") if data_dir is None else data_dir,
        platform=platform,
        default_windows_root=default_windows_root,
    )


_ROOT_SELECTION = _resolve_root()
MARKET_ROOT = _ROOT_SELECTION.root
RAW_DIR = MARKET_ROOT / "raw"
KLINES_DIR = RAW_DIR / "klines_json"
FUNDING_DIR = RAW_DIR / "funding"
PARQUET_DIR = MARKET_ROOT / "normalized" / "ohlcv_parquet"
FEATURES_DIR = MARKET_ROOT / "processed" / "features_app"
FEATURES_ML_DIR = MARKET_ROOT / "processed" / "features_ml"
LABELS_DIR = MARKET_ROOT / "processed" / "labels"
MODELS_ROOT = MARKET_ROOT / "models"
MODELS_CURRENT_DIR = MODELS_ROOT / "current"
ACTIVE_MODEL_RELEASE_FILE = MODELS_CURRENT_DIR / "ACTIVE"
MODELS_RELEASES_DIR = MODELS_ROOT / "releases"
MODELS_ARCHIVE_DIR = MODELS_ROOT / "archive"
SUPERVISED_CANDIDATES_DIR = MODELS_ROOT / "candidates" / "supervised"
EXPERIMENTS_DIR = MARKET_ROOT / "experiments"
BACKTEST_DIR = EXPERIMENTS_DIR / "backtests"
REPORTS_DIR = EXPERIMENTS_DIR / "reports"
EXPERIMENTS_DB = EXPERIMENTS_DIR / "experiments.db"
JOURNAL_DIR = MARKET_ROOT / "runtime" / "journal"
REGIME_DIR = MARKET_ROOT / "runtime" / "regime_cache"


def resolve_current_model_release() -> Path:
    """Resolve one immutable active release without mutating model storage."""
    current_root = MODELS_CURRENT_DIR.resolve(strict=True)
    active_file = ACTIVE_MODEL_RELEASE_FILE
    if active_file.is_file():
        release_id = active_file.read_text(encoding="utf-8").strip()
        if not _RELEASE_ID_PATTERN.fullmatch(release_id):
            raise ValueError(f"invalid active model release id: {release_id!r}")
        for release_root in (MODELS_RELEASES_DIR, MODELS_CURRENT_DIR):
            release_path = release_root / release_id
            if not release_path.exists():
                continue
            if release_path.is_symlink():
                raise ValueError(
                    f"active model release cannot be a symlink: {release_path}"
                )
            release_root = release_root.resolve(strict=True)
            release = release_path.resolve(strict=True)
            if release.parent != release_root or not release.is_dir():
                raise ValueError(f"active model release is invalid: {release}")
            return release
        raise FileNotFoundError(f"active model release does not exist: {release_id}")

    releases = sorted(
        path.resolve(strict=True)
        for path in current_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    if len(releases) != 1:
        raise RuntimeError(
            "models/current must contain ACTIVE or exactly one release directory; "
            f"found {len(releases)}"
        )
    return releases[0]


def validate_supervised_candidate_dir(
    path: str | Path, *, create: bool = False
) -> Path:
    """Validate a release directory contained by supervised candidates."""
    candidate_root = SUPERVISED_CANDIDATES_DIR.resolve(strict=False)
    candidate = Path(path).expanduser().resolve(strict=False)
    if candidate == candidate_root:
        raise ValueError("a supervised candidate release directory is required")
    try:
        candidate.relative_to(candidate_root)
    except ValueError as exc:
        raise ValueError(
            f"supervised model output must be under {candidate_root}: {candidate}"
        ) from exc
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    elif not candidate.is_dir():
        raise FileNotFoundError(f"supervised candidate does not exist: {candidate}")
    return candidate


def supervised_candidate_dir(release_id: str, *, create: bool = False) -> Path:
    """Resolve a safe, versioned supervised candidate release directory."""
    if not _RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ValueError(
            "release_id must contain only letters, digits, dot, underscore, or hyphen"
        )
    return validate_supervised_candidate_dir(
        SUPERVISED_CANDIDATES_DIR / release_id,
        create=create,
    )

# Runtime state is the only application-owned structure created automatically.
for _runtime_dir in (JOURNAL_DIR, REGIME_DIR):
    _runtime_dir.mkdir(parents=True, exist_ok=True)
