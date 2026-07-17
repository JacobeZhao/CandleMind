"""Pure, versioned decision contract for the ML trend strategy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


TREND_DECISION_VERSION = "ml_trend_v1"
LARGE_CAP_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT"})


@dataclass(frozen=True)
class TrendDecisionParams:
    contract_version: str = TREND_DECISION_VERSION
    entry_long_threshold: float = 0.51
    entry_short_threshold: float = 0.51
    min_prob_gap: float = 0.02
    min_prob_gap_large_cap: float = 0.06
    allowed_direction: int = 0
    short_extra_delta: float = 0.0
    vol_gate: bool = True
    ema_align_gate: bool = True
    hurst_gate: bool = True
    hurst_entry_min: float = 0.50
    monthly_trend_filter: bool = True
    trend_bias_delta: float = 0.08
    exit_threshold: float = 0.38
    reversal_threshold: float = 0.55
    time_weighted_exit: bool = True
    time_exit_bars: int = 12
    time_exit_delta: float = 0.05
    min_hold_bars: int = 0


@dataclass(frozen=True)
class TrendFeatureSnapshot:
    long_prob: float
    short_prob: float
    close: float
    vol_regime: float = 1.0
    ema_align: float = 0.0
    hurst: float = 0.99
    monthly_sma: Optional[float] = None
    model_available: bool = True
    feature_fresh: bool = True
    feature_timestamp: Optional[str] = None

    @property
    def ml_usable(self) -> bool:
        return self.model_available and self.feature_fresh


@dataclass(frozen=True)
class TrendPositionSnapshot:
    direction: int
    bars_held: int


@dataclass(frozen=True)
class DecisionIntent:
    contract_version: str
    action: str
    reason_code: str
    direction: int = 0
    probability: float = 0.0
    exit_threshold: Optional[float] = None
    reversal_threshold: Optional[float] = None


def decide_entry(
    symbol: str,
    snapshot: TrendFeatureSnapshot,
    params: TrendDecisionParams,
) -> DecisionIntent:
    """Return an entry intent without performing execution or state mutation."""
    _validate_version(params)
    unavailable = _unavailable_reason(snapshot)
    if unavailable:
        return _hold(unavailable)
    gap_required = (
        params.min_prob_gap_large_cap
        if symbol.upper() in LARGE_CAP_SYMBOLS
        else params.min_prob_gap
    )
    monthly_available = (
        params.monthly_trend_filter
        and snapshot.monthly_sma is not None
        and math.isfinite(snapshot.monthly_sma)
    )
    above_monthly_sma = monthly_available and snapshot.close > snapshot.monthly_sma

    base_long_threshold = params.entry_long_threshold
    base_short_threshold = params.entry_short_threshold + params.short_extra_delta
    effective_long_threshold = base_long_threshold
    effective_short_threshold = base_short_threshold
    if monthly_available:
        if above_monthly_sma:
            effective_short_threshold += params.trend_bias_delta
        else:
            effective_long_threshold += params.trend_bias_delta

    long_allowed = params.allowed_direction >= 0
    short_allowed = params.allowed_direction <= 0
    long_gap = snapshot.long_prob - snapshot.short_prob >= gap_required
    short_gap = snapshot.short_prob - snapshot.long_prob >= gap_required
    base_long = long_allowed and snapshot.long_prob >= base_long_threshold and long_gap
    base_short = short_allowed and snapshot.short_prob >= base_short_threshold and short_gap
    can_long = long_allowed and snapshot.long_prob >= effective_long_threshold and long_gap
    can_short = short_allowed and snapshot.short_prob >= effective_short_threshold and short_gap

    if not can_long and not can_short:
        if monthly_available and (base_long or base_short):
            reason = "monthly_trend_gate"
        elif (
            (long_allowed and snapshot.long_prob >= effective_long_threshold)
            or (short_allowed and snapshot.short_prob >= effective_short_threshold)
        ):
            reason = "min_prob_gap"
        elif not long_allowed and not short_allowed:
            reason = "direction_gate"
        else:
            reason = "entry_probability"
        return _hold(reason)

    if params.vol_gate and snapshot.vol_regime >= 2.0:
        return _hold("vol_gate")

    if params.ema_align_gate:
        can_long = can_long and snapshot.ema_align >= 1.0
        can_short = can_short and snapshot.ema_align <= -1.0
        if not can_long and not can_short:
            return _hold("ema_align_gate")

    if params.hurst_gate and snapshot.hurst < params.hurst_entry_min:
        return _hold("hurst_gate")

    direction = 1 if can_long else -1
    probability = snapshot.long_prob if direction == 1 else snapshot.short_prob
    return DecisionIntent(
        contract_version=TREND_DECISION_VERSION,
        action="entry",
        reason_code="entry_long" if direction == 1 else "entry_short",
        direction=direction,
        probability=probability,
    )


def decide_ml_exit(
    snapshot: TrendFeatureSnapshot,
    position: TrendPositionSnapshot,
    params: TrendDecisionParams,
) -> DecisionIntent:
    """Return only ML exit/reversal intent; risk exits remain engine-owned."""
    _validate_version(params)
    if position.direction not in (-1, 1):
        raise ValueError("position.direction must be -1 or 1")
    unavailable = _unavailable_reason(snapshot)
    if unavailable:
        return _hold(unavailable)

    bars_held = max(0, int(position.bars_held))
    if params.time_weighted_exit:
        age_fraction = min(1.0, bars_held / max(1, params.time_exit_bars))
        adjustment = params.time_exit_delta * (1.0 - age_fraction)
        exit_threshold = params.exit_threshold + adjustment
        reversal_threshold = params.reversal_threshold - adjustment
    else:
        exit_threshold = params.exit_threshold
        reversal_threshold = params.reversal_threshold

    same_prob = (
        snapshot.long_prob if position.direction == 1 else snapshot.short_prob
    )
    opposite_prob = (
        snapshot.short_prob if position.direction == 1 else snapshot.long_prob
    )
    reversal = opposite_prob >= reversal_threshold
    ml_exit = same_prob < exit_threshold

    if bars_held < params.min_hold_bars and (reversal or ml_exit):
        return DecisionIntent(
            contract_version=TREND_DECISION_VERSION,
            action="hold",
            reason_code="min_hold_gate",
            exit_threshold=exit_threshold,
            reversal_threshold=reversal_threshold,
        )
    if reversal:
        return DecisionIntent(
            contract_version=TREND_DECISION_VERSION,
            action="ml_reversal",
            reason_code="ml_reversal",
            direction=position.direction,
            probability=same_prob,
            exit_threshold=exit_threshold,
            reversal_threshold=reversal_threshold,
        )
    if ml_exit:
        return DecisionIntent(
            contract_version=TREND_DECISION_VERSION,
            action="ml_exit",
            reason_code="ml_exit",
            direction=position.direction,
            probability=same_prob,
            exit_threshold=exit_threshold,
            reversal_threshold=reversal_threshold,
        )
    return DecisionIntent(
        contract_version=TREND_DECISION_VERSION,
        action="hold",
        reason_code="hold",
        direction=position.direction,
        probability=same_prob,
        exit_threshold=exit_threshold,
        reversal_threshold=reversal_threshold,
    )


def _hold(reason_code: str) -> DecisionIntent:
    return DecisionIntent(
        contract_version=TREND_DECISION_VERSION,
        action="hold",
        reason_code=reason_code,
    )


def _unavailable_reason(snapshot: TrendFeatureSnapshot) -> Optional[str]:
    if not snapshot.model_available:
        return "model_unavailable"
    if not snapshot.feature_fresh:
        return "feature_stale"
    return None


def _validate_version(params: TrendDecisionParams) -> None:
    if params.contract_version != TREND_DECISION_VERSION:
        raise ValueError(
            f"unsupported trend decision contract: {params.contract_version!r}"
        )
