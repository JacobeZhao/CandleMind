"""Compact causal EMA features for the EMA trend V2 challenger.

The builder accepts one continuous stream of completed raw five-minute OHLC
bars. EMA price levels are temporary calculation state and are never returned.
Feature ablations are predeclared here; data-dependent feature selection must
be performed separately inside a training fold.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


EMA_V2_FEATURE_SET = "ema_trend_v2_compact"
BASE_BAR = pd.Timedelta(minutes=5)
TIMEFRAME_RULES: Mapping[str, str] = {
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}
TIMEFRAME_HOURS: Mapping[str, float] = {
    "5m": 1.0 / 12.0,
    "15m": 0.25,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
}

FEATURE_FAMILIES = (
    "distance",
    "spreads",
    "slopes",
    "acceleration",
    "order_run",
    "agreement",
)

# These names are experiment identifiers, not results of feature statistics.
FAMILY_ABLATIONS: Mapping[str, tuple[str, ...]] = {
    "full": (),
    "no_distance": ("distance",),
    "no_spreads": ("spreads",),
    "no_slopes": ("slopes",),
    "no_acceleration": ("acceleration",),
    "no_order_run": ("order_run",),
    "no_agreement": ("agreement",),
}
TIMEFRAME_ABLATIONS: Mapping[str, tuple[str, ...]] = {
    "full": (),
    "no_5m": ("5m",),
    "no_15m": ("15m",),
    "no_1h": ("1h",),
    "no_4h": ("4h",),
    "no_1d": ("1d",),
    "slow_only": ("5m", "15m"),
}


@dataclass(frozen=True)
class EmaFeatureV2Config:
    timeframes: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
    ema_lengths: tuple[int, ...] = (8, 21, 55, 200)
    anchor_length: int = 21
    slope_lengths: tuple[int, ...] = (21, 55, 200)
    acceleration_lengths: tuple[int, ...] = (21, 55)
    slope_lag: int = 3
    feature_families: tuple[str, ...] = FEATURE_FAMILIES
    ablation_name: str = "full"

    def validate(self) -> None:
        if not self.timeframes:
            raise ValueError("timeframes must not be empty")
        if tuple(dict.fromkeys(self.timeframes)) != self.timeframes:
            raise ValueError("timeframes must be unique")
        unknown_timeframes = set(self.timeframes) - set(TIMEFRAME_RULES)
        if unknown_timeframes:
            raise ValueError(f"unknown EMA timeframes: {sorted(unknown_timeframes)}")
        if len(self.ema_lengths) < 2 or tuple(sorted(set(self.ema_lengths))) != self.ema_lengths:
            raise ValueError("ema_lengths must be unique and increasing")
        if any(length <= 1 for length in self.ema_lengths):
            raise ValueError("EMA lengths must be greater than one")
        if self.anchor_length not in self.ema_lengths:
            raise ValueError("anchor_length must be one of ema_lengths")
        if not self.slope_lengths or not set(self.slope_lengths).issubset(self.ema_lengths):
            raise ValueError("slope_lengths must be a non-empty subset of ema_lengths")
        if not self.acceleration_lengths or not set(self.acceleration_lengths).issubset(
            self.slope_lengths
        ):
            raise ValueError("acceleration_lengths must be a non-empty subset of slope_lengths")
        if self.slope_lag <= 0:
            raise ValueError("slope_lag must be positive")
        if not self.feature_families:
            raise ValueError("feature_families must not be empty")
        if tuple(dict.fromkeys(self.feature_families)) != self.feature_families:
            raise ValueError("feature_families must be unique")
        unknown_families = set(self.feature_families) - set(FEATURE_FAMILIES)
        if unknown_families:
            raise ValueError(f"unknown EMA feature families: {sorted(unknown_families)}")
        if not self.ablation_name.strip():
            raise ValueError("ablation_name must not be empty")


@dataclass(frozen=True)
class EmaFeatureV2Result:
    bars: pd.DataFrame
    feature_columns: tuple[str, ...]
    family_columns: Mapping[str, tuple[str, ...]]
    config: EmaFeatureV2Config


@dataclass(frozen=True)
class EmaV2ScalerScope:
    fold_id: str
    train_start: str | pd.Timestamp
    train_end: str | pd.Timestamp
    symbols: tuple[str, ...]

    def validate(self) -> None:
        if not self.fold_id.strip():
            raise ValueError("fold_id must not be empty")
        if _utc_timestamp(self.train_end) <= _utc_timestamp(self.train_start):
            raise ValueError("scaler train_end must be after train_start")
        if not self.symbols or tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("scaler symbols must be unique and sorted")


def ema_v2_ablation_config(
    *,
    family_ablation: str = "full",
    timeframe_ablation: str = "full",
    config: EmaFeatureV2Config | None = None,
) -> EmaFeatureV2Config:
    """Resolve a predeclared family/timeframe ablation without inspecting data."""

    config = config or EmaFeatureV2Config()
    config.validate()
    if family_ablation not in FAMILY_ABLATIONS:
        raise ValueError(f"unknown family ablation: {family_ablation}")
    if timeframe_ablation not in TIMEFRAME_ABLATIONS:
        raise ValueError(f"unknown timeframe ablation: {timeframe_ablation}")
    removed_families = set(FAMILY_ABLATIONS[family_ablation])
    removed_timeframes = set(TIMEFRAME_ABLATIONS[timeframe_ablation])
    families = tuple(f for f in config.feature_families if f not in removed_families)
    timeframes = tuple(t for t in config.timeframes if t not in removed_timeframes)
    if not families:
        raise ValueError("ablation removes every feature family")
    if not timeframes:
        raise ValueError("ablation removes every timeframe")
    name = f"family={family_ablation};timeframe={timeframe_ablation}"
    resolved = replace(
        config,
        feature_families=families,
        timeframes=timeframes,
        ablation_name=name,
    )
    resolved.validate()
    return resolved


def ema_v2_family_columns(
    config: EmaFeatureV2Config | None = None,
) -> dict[str, tuple[str, ...]]:
    config = config or EmaFeatureV2Config()
    config.validate()
    by_family: dict[str, tuple[str, ...]] = {}
    adjacent = tuple(zip(config.ema_lengths[:-1], config.ema_lengths[1:]))
    for family in config.feature_families:
        columns: list[str] = []
        if family == "agreement":
            columns.append("cross_timeframe_order_agreement")
        else:
            for timeframe in config.timeframes:
                if family == "distance":
                    columns.append(f"{timeframe}_close_to_ema{config.anchor_length}_log")
                elif family == "spreads":
                    columns.extend(
                        f"{timeframe}_ema{fast}_to_ema{slow}_log"
                        for fast, slow in adjacent
                    )
                elif family == "slopes":
                    columns.extend(
                        f"{timeframe}_ema{length}_slope_per_hour"
                        for length in config.slope_lengths
                    )
                elif family == "acceleration":
                    columns.extend(
                        f"{timeframe}_ema{length}_acceleration_per_hour2"
                        for length in config.acceleration_lengths
                    )
                elif family == "order_run":
                    columns.append(f"{timeframe}_full_order_run_length")
        by_family[family] = tuple(columns)
    return by_family


def ema_v2_feature_columns(
    config: EmaFeatureV2Config | None = None,
) -> tuple[str, ...]:
    families = ema_v2_family_columns(config)
    return tuple(column for columns in families.values() for column in columns)


def build_ema_feature_frame_v2(
    raw_bars: pd.DataFrame,
    *,
    as_of: str | pd.Timestamp,
    config: EmaFeatureV2Config | None = None,
    output_start: str | pd.Timestamp | None = None,
    output_end: str | pd.Timestamp | None = None,
) -> EmaFeatureV2Result:
    """Build observations available after each completed five-minute candle."""

    config = config or EmaFeatureV2Config()
    config.validate()
    bars = _prepare_raw_bars(raw_bars, as_of=_utc_timestamp(as_of))
    decision = bars.copy()
    decision["decision_available_at"] = decision["open_time"] + BASE_BAR
    order_columns: list[str] = []

    for timeframe in config.timeframes:
        completed = _completed_timeframe_bars(bars, timeframe)
        features = _timeframe_features(completed, timeframe, config)
        order_column = f"_{timeframe}_full_order_direction"
        order_columns.append(order_column)
        decision = pd.merge_asof(
            decision.sort_values("decision_available_at"),
            features,
            left_on="decision_available_at",
            right_on=f"{timeframe}_available_at",
            direction="backward",
            allow_exact_matches=True,
        )

    if "agreement" in config.feature_families:
        decision["cross_timeframe_order_agreement"] = decision[order_columns].mean(axis=1)
    decision = decision.drop(columns=order_columns)
    family_columns = ema_v2_family_columns(config)
    columns = tuple(column for family in family_columns.values() for column in family)
    decision = decision.replace([np.inf, -np.inf], np.nan)
    decision = decision.dropna(subset=list(columns)).reset_index(drop=True)
    if output_start is not None:
        decision = decision[
            decision["decision_available_at"] >= _utc_timestamp(output_start)
        ]
    if output_end is not None:
        decision = decision[
            decision["decision_available_at"] < _utc_timestamp(output_end)
        ]
    decision = decision.reset_index(drop=True)
    if decision.empty:
        raise ValueError("no EMA V2 rows remain after warm-up and output bounds")
    _validate_availability(decision, config)
    if any(_looks_like_raw_ema_level(column) for column in columns):
        raise AssertionError("EMA V2 schema exposes a raw EMA price level")
    return EmaFeatureV2Result(decision, columns, family_columns, config)


def fit_ema_v2_scaler(
    bars: pd.DataFrame,
    feature_columns: Iterable[str],
    *,
    scope: EmaV2ScalerScope,
    clip: float = 10.0,
    near_zero_mad: float = 1e-8,
) -> dict[str, Any]:
    """Fit an equal-symbol median/MAD scaler on one declared training fold."""

    columns = tuple(feature_columns)
    if not columns or len(set(columns)) != len(columns):
        raise ValueError("feature_columns must be non-empty and unique")
    if not np.isfinite(clip) or clip <= 0.0:
        raise ValueError("clip must be finite and positive")
    if not np.isfinite(near_zero_mad) or near_zero_mad <= 0.0:
        raise ValueError("near_zero_mad must be finite and positive")
    _validate_scaler_rows(bars, columns, scope)

    symbols = bars["symbol"].astype(str)
    stats: dict[str, dict[str, float | bool]] = {}
    for column in columns:
        values = pd.to_numeric(bars[column], errors="raise").astype(float)
        if not np.isfinite(values).all():
            raise ValueError(f"non-finite EMA V2 feature: {column}")
        symbol_medians = values.groupby(symbols, sort=True).median()
        center = float(symbol_medians.median())
        symbol_mads = (
            (values - center).abs().groupby(symbols, sort=True).median()
        )
        mad = float(symbol_mads.median())
        robust_scale = 1.4826 * mad
        near_zero = not np.isfinite(robust_scale) or robust_scale < near_zero_mad
        stats[column] = {
            "median": center,
            "mad": mad,
            "scale": 1.0 if near_zero else robust_scale,
            "near_zero_variance": near_zero,
        }

    start = _utc_timestamp(scope.train_start)
    end = _utc_timestamp(scope.train_end)
    payload: dict[str, Any] = {
        "version": "ema_v2_equal_symbol_mad_v1",
        "feature_set": EMA_V2_FEATURE_SET,
        "schema": list(columns),
        "scope": {
            "fold_id": scope.fold_id,
            "train_start": start.isoformat(),
            "train_end": end.isoformat(),
            "symbols": list(scope.symbols),
            "row_count": int(len(bars)),
            "rows_by_symbol": {
                symbol: int(count)
                for symbol, count in symbols.value_counts(sort=False).sort_index().items()
            },
            "source_data_sha256": _scaler_data_hash(bars, columns),
        },
        "parameters": {
            "center": "median_of_symbol_medians",
            "dispersion": "median_of_symbol_mads_about_center",
            "mad_consistency_factor": 1.4826,
            "clip": float(clip),
            "near_zero_mad": float(near_zero_mad),
        },
        "features": stats,
    }
    payload["provenance_sha256"] = _provenance_hash(payload)
    return payload


def apply_ema_v2_scaler(
    bars: pd.DataFrame,
    scaler: Mapping[str, Any],
    feature_columns: Iterable[str],
    *,
    suffix: str = "_z",
    expected_fold_id: str | None = None,
) -> pd.DataFrame:
    columns = tuple(feature_columns)
    _require_columns(bars, columns)
    if not suffix:
        raise ValueError("suffix must not be empty")
    _validate_scaler_artifact(scaler, columns, expected_fold_id=expected_fold_id)
    configured = scaler["features"]
    clip = float(scaler["parameters"]["clip"])
    out = bars.copy()
    for column in columns:
        values = pd.to_numeric(out[column], errors="raise").astype(float)
        if not np.isfinite(values).all():
            raise ValueError(f"non-finite EMA V2 feature: {column}")
        stats = configured[column]
        out[f"{column}{suffix}"] = (
            (values - float(stats["median"])) / float(stats["scale"])
        ).clip(-clip, clip)
    scaled = tuple(f"{column}{suffix}" for column in columns)
    if not np.isfinite(out.loc[:, scaled].to_numpy(dtype=float)).all():
        raise ValueError("scaled EMA V2 features contain non-finite values")
    return out


def diagnose_ema_v2_features(
    bars: pd.DataFrame,
    feature_columns: Iterable[str],
    *,
    scope: EmaV2ScalerScope,
    near_zero_mad: float = 1e-8,
    correlation_threshold: float = 0.95,
) -> dict[str, Any]:
    """Report equal-symbol robust dispersion and weighted correlations.

    This function reports diagnostics only. It intentionally does not return a
    selected schema, keeping any pruning decision scoped to the training fold.
    """

    columns = tuple(feature_columns)
    if not columns or len(set(columns)) != len(columns):
        raise ValueError("feature_columns must be non-empty and unique")
    _validate_scaler_rows(bars, columns, scope)
    if not 0.0 < correlation_threshold <= 1.0:
        raise ValueError("correlation_threshold must be in (0, 1]")
    symbols = bars["symbol"].astype(str)
    if symbols.empty or symbols.str.len().eq(0).any():
        raise ValueError("diagnostics require non-empty symbols")
    numeric = bars.loc[:, columns].apply(pd.to_numeric, errors="raise").astype(float)
    if not np.isfinite(numeric.to_numpy()).all():
        raise ValueError("diagnostics require finite feature values")

    near_zero: list[dict[str, Any]] = []
    for column in columns:
        values = numeric[column]
        center = float(values.groupby(symbols, sort=True).median().median())
        mad = float((values - center).abs().groupby(symbols, sort=True).median().median())
        if 1.4826 * mad < near_zero_mad:
            near_zero.append({"feature": column, "median": center, "mad": mad})

    counts = symbols.value_counts()
    weights = symbols.map(lambda symbol: 1.0 / float(counts[symbol])).to_numpy()
    weights /= weights.sum()
    matrix = numeric.to_numpy(dtype=float)
    means = np.sum(matrix * weights[:, None], axis=0)
    centered = matrix - means
    covariance = (centered * weights[:, None]).T @ centered
    variances = np.diag(covariance)
    denominator = np.sqrt(np.outer(variances, variances))
    correlation = np.divide(
        covariance,
        denominator,
        out=np.full_like(covariance, np.nan),
        where=denominator > 0.0,
    )
    high_correlation: list[dict[str, Any]] = []
    for left in range(len(columns)):
        for right in range(left + 1, len(columns)):
            value = float(correlation[left, right])
            if np.isfinite(value) and abs(value) >= correlation_threshold:
                high_correlation.append(
                    {"left": columns[left], "right": columns[right], "correlation": value}
                )
    return {
        "fold_id": scope.fold_id,
        "source_data_sha256": _scaler_data_hash(bars, columns),
        "row_count": int(len(bars)),
        "symbol_count": int(symbols.nunique()),
        "near_zero_mad_threshold": float(near_zero_mad),
        "correlation_threshold": float(correlation_threshold),
        "near_zero_variance": near_zero,
        "high_correlation": high_correlation,
    }


def _prepare_raw_bars(raw_bars: pd.DataFrame, *, as_of: pd.Timestamp) -> pd.DataFrame:
    required = ("open_time", "open", "high", "low", "close")
    _require_columns(raw_bars, required)
    bars = raw_bars.loc[:, required].copy()
    bars["open_time"] = _utc_series(bars["open_time"])
    if bars["open_time"].isna().any():
        raise ValueError("raw bars contain invalid open_time values")
    if bars["open_time"].duplicated().any():
        raise ValueError("raw bars contain duplicate open_time values")
    bars = bars.sort_values("open_time").reset_index(drop=True)
    if len(bars) < 2:
        raise ValueError("at least two raw bars are required")
    epoch_ns = bars["open_time"].astype("int64")
    if (epoch_ns % int(BASE_BAR.value)).any():
        raise ValueError("raw bars are not aligned to the five-minute UTC grid")
    if not bars["open_time"].diff().dropna().eq(BASE_BAR).all():
        raise ValueError("raw bars must form an uninterrupted five-minute grid")
    if (bars["open_time"] + BASE_BAR > as_of).any():
        raise ValueError("raw bars include a candle that is incomplete at as_of")
    latest_complete_open = as_of.floor(BASE_BAR) - BASE_BAR
    if bars["open_time"].iloc[-1] != latest_complete_open:
        raise ValueError("raw bars do not cover the latest candle completed at as_of")
    for column in ("open", "high", "low", "close"):
        bars[column] = pd.to_numeric(bars[column], errors="raise").astype(float)
    prices = bars.loc[:, ("open", "high", "low", "close")].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0.0).any():
        raise ValueError("raw OHLC prices must be finite and positive")
    if (bars["high"] < bars[["open", "close"]].max(axis=1)).any():
        raise ValueError("bar high is below open or close")
    if (bars["low"] > bars[["open", "close"]].min(axis=1)).any():
        raise ValueError("bar low is above open or close")
    return bars


def _completed_timeframe_bars(bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = TIMEFRAME_RULES[timeframe]
    expected = int(pd.Timedelta(rule) / BASE_BAR)
    indexed = bars.set_index("open_time")
    grouped = indexed.resample(rule, label="right", closed="left", origin="epoch")
    completed = grouped.agg(
        close=("close", "last"),
        source_bars=("close", "count"),
    )
    completed = completed[completed["source_bars"] == expected].reset_index()
    completed = completed.rename(columns={"open_time": f"{timeframe}_available_at"})
    if completed.empty:
        raise ValueError(f"no complete {timeframe} candles available")
    return completed


def _timeframe_features(
    completed: pd.DataFrame,
    timeframe: str,
    config: EmaFeatureV2Config,
) -> pd.DataFrame:
    available = f"{timeframe}_available_at"
    close = completed["close"].astype(float)
    ema: dict[int, pd.Series] = {
        length: close.ewm(span=length, adjust=False, min_periods=length).mean()
        for length in config.ema_lengths
    }
    out = completed.loc[:, [available]].copy()
    if "distance" in config.feature_families:
        out[f"{timeframe}_close_to_ema{config.anchor_length}_log"] = np.log(
            close / ema[config.anchor_length]
        )
    if "spreads" in config.feature_families:
        for fast, slow in zip(config.ema_lengths[:-1], config.ema_lengths[1:]):
            out[f"{timeframe}_ema{fast}_to_ema{slow}_log"] = np.log(
                ema[fast] / ema[slow]
            )

    slopes: dict[int, pd.Series] = {}
    elapsed_hours = config.slope_lag * TIMEFRAME_HOURS[timeframe]
    for length in config.slope_lengths:
        slopes[length] = np.log(ema[length] / ema[length].shift(config.slope_lag)) / elapsed_hours
        if "slopes" in config.feature_families:
            out[f"{timeframe}_ema{length}_slope_per_hour"] = slopes[length]
    if "acceleration" in config.feature_families:
        for length in config.acceleration_lengths:
            out[f"{timeframe}_ema{length}_acceleration_per_hour2"] = (
                slopes[length].diff() / TIMEFRAME_HOURS[timeframe]
            )

    ordered = pd.concat([ema[length] for length in config.ema_lengths], axis=1)
    bullish = ordered.diff(axis=1).iloc[:, 1:].lt(0.0).all(axis=1)
    bearish = ordered.diff(axis=1).iloc[:, 1:].gt(0.0).all(axis=1)
    direction = pd.Series(
        np.select((bullish, bearish), (1, -1), default=0),
        index=completed.index,
        dtype=float,
    )
    direction[ordered.isna().any(axis=1)] = np.nan
    run = _signed_run_length(direction)
    if "order_run" in config.feature_families:
        out[f"{timeframe}_full_order_run_length"] = run
    out[f"_{timeframe}_full_order_direction"] = direction
    return out


def _signed_run_length(direction: pd.Series) -> pd.Series:
    result = np.zeros(len(direction), dtype=float)
    previous = 0.0
    count = 0
    for index, value in enumerate(direction.to_numpy(dtype=float)):
        if not np.isfinite(value):
            result[index] = np.nan
            previous = 0.0
            count = 0
        elif value == 0.0:
            result[index] = 0.0
            previous = 0.0
            count = 0
        else:
            count = count + 1 if value == previous else 1
            result[index] = value * count
            previous = value
    return pd.Series(result, index=direction.index)


def _validate_availability(bars: pd.DataFrame, config: EmaFeatureV2Config) -> None:
    for timeframe in config.timeframes:
        available = bars[f"{timeframe}_available_at"]
        if available.isna().any() or (available > bars["decision_available_at"]).any():
            raise AssertionError(f"{timeframe} EMA feature uses an incomplete candle")


def _validate_scaler_rows(
    bars: pd.DataFrame,
    columns: tuple[str, ...],
    scope: EmaV2ScalerScope,
) -> None:
    scope.validate()
    _require_columns(bars, ("decision_available_at", "symbol", *columns))
    if bars.empty:
        raise ValueError("scaler training rows must not be empty")
    decisions = _utc_series(bars["decision_available_at"])
    start = _utc_timestamp(scope.train_start)
    end = _utc_timestamp(scope.train_end)
    if ((decisions < start) | (decisions >= end)).any():
        raise ValueError("scaler rows escape the declared training interval")
    symbols = bars["symbol"].astype(str)
    if symbols.str.len().eq(0).any() or tuple(sorted(symbols.unique())) != scope.symbols:
        raise ValueError("scaler rows must contain every and only declared training symbol")
    if pd.DataFrame(
        {"decision_available_at": decisions, "symbol": symbols}
    ).duplicated().any():
        raise ValueError("scaler rows contain duplicate symbol decision timestamps")


def _validate_scaler_artifact(
    scaler: Mapping[str, Any],
    columns: tuple[str, ...],
    *,
    expected_fold_id: str | None,
) -> None:
    if scaler.get("version") != "ema_v2_equal_symbol_mad_v1":
        raise ValueError("unsupported EMA V2 scaler version")
    if tuple(scaler.get("schema", ())) != columns:
        raise ValueError("EMA V2 scaler schema does not match feature columns")
    scope = scaler.get("scope")
    if not isinstance(scope, Mapping) or not scope.get("fold_id"):
        raise ValueError("EMA V2 scaler is missing fold scope")
    if expected_fold_id is not None and scope["fold_id"] != expected_fold_id:
        raise ValueError("EMA V2 scaler fold does not match requested fold")
    features = scaler.get("features")
    if not isinstance(features, Mapping) or set(features) != set(columns):
        raise ValueError("EMA V2 scaler feature statistics do not match schema")
    expected_hash = scaler.get("provenance_sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("EMA V2 scaler is missing immutable provenance")
    artifact = dict(scaler)
    artifact.pop("provenance_sha256", None)
    if not hashlib.sha256(_canonical_json(artifact)).hexdigest() == expected_hash:
        raise ValueError("EMA V2 scaler provenance hash mismatch")
    for column in columns:
        stats = features[column]
        try:
            values = (float(stats["median"]), float(stats["mad"]), float(stats["scale"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid EMA V2 scaler statistics: {column}") from exc
        if not np.isfinite(values).all() or values[1] < 0.0 or values[2] <= 0.0:
            raise ValueError(f"invalid EMA V2 scaler statistics: {column}")


def _looks_like_raw_ema_level(column: str) -> bool:
    return "ema" in column and not any(
        marker in column
        for marker in ("close_to_", "_to_ema", "slope", "acceleration", "order")
    )


def _require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")


def _utc_series(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_datetime(values, unit="ms", utc=True, errors="coerce")
    return pd.to_datetime(values, utc=True, errors="coerce")


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("timestamp boundary must not be NaT")
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _scaler_data_hash(bars: pd.DataFrame, columns: tuple[str, ...]) -> str:
    canonical = bars.loc[:, ["decision_available_at", "symbol", *columns]].copy()
    canonical["decision_available_at"] = _utc_series(canonical["decision_available_at"])
    canonical["symbol"] = canonical["symbol"].astype(str)
    canonical = canonical.sort_values(["symbol", "decision_available_at"]).reset_index(drop=True)
    hashed = pd.util.hash_pandas_object(canonical, index=False).to_numpy(dtype="uint64")
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _provenance_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()
