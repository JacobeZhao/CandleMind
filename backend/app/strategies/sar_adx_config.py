"""Frozen production-facing configuration for the SAR/ADX V3 strategy."""

from __future__ import annotations

import hashlib
import json

from .sar_pyramid import SarPyramidConfig, config_payload


CONFIG_VERSION = "sar_adx_v3"


def sar_adx_v3_config(*, initial_cash: float = 10_000.0) -> SarPyramidConfig:
    config = SarPyramidConfig(
        initial_cash=initial_cash,
        target_notional_fraction=1.0,
        layers=5,
        sar_step=0.02,
        sar_max=0.20,
        fee_rate=0.001,
        slippage_rate=0.0002,
        use_adx_filter=True,
        adx_timeframe="1h",
        adx_period=14,
        adx_threshold=45.0,
        adx_rising_periods=2,
        entry_confirmation_bars=6,
        recapture_buffer_fraction=0.0024,
        require_progressive_adds=True,
        max_entries_per_adx_regime=2,
    )
    config.validate()
    return config


def config_hash(config: SarPyramidConfig) -> str:
    payload = json.dumps(config_payload(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
