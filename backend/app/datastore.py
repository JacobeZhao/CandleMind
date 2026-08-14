"""Central market-data paths with fail-closed authoritative root selection."""

from __future__ import annotations

import os
from pathlib import Path

from .data_layout import DataRootSelection, select_data_root


_DEFAULT_CLEAN = Path("G:/CandleMind/CandleMind_data")


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
