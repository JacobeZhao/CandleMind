"""Neutral read-only access to the retained historical market release contract.

Phase 1 deliberately delegates OHLCV and PIT validation to the unchanged EMA V2
verifier. This facade gives SAR callers a product-neutral API without claiming
that the historical release implementation has been fully decoupled yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ema_data_release import verify_ema_data_release


def verify_market_release(release_dir: Path) -> dict[str, Any]:
    """Verify an existing market release using its historical V2 contract."""

    return verify_ema_data_release(release_dir)


__all__ = ["verify_market_release"]
