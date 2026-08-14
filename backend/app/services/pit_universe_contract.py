"""Neutral point-in-time universe contract for historical EMA releases."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import pandas as pd


EMA_UNIVERSE_SCHEMA = "ema_point_in_time_universe_v2"
UNIVERSE_COLUMNS = (
    "decision_time",
    "effective_from",
    "effective_to",
    "symbol",
    "eligible",
    "rank",
    "missing_reason",
    "available_at",
    "trailing_window_end",
    "trailing_quote_volume",
    "data_complete",
    "fee_available",
    "funding_available",
    "cost_available",
    "liquidity_rule",
    "minimum_quote_volume",
    "top_n",
    "minimum_seasoning_seconds",
    "listing_source_id",
    "snapshot_source_id",
    "rule_source_id",
)


def ema_universe_content_hash(universe_snapshots: pd.DataFrame) -> str:
    """Return an order-independent SHA-256 over the canonical release content."""

    canonical = _canonical_universe_records(universe_snapshots)
    payload = {"schema": EMA_UNIVERSE_SCHEMA, "rows": canonical}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validate_universe_frame(frame: pd.DataFrame) -> None:
    if tuple(frame.columns) != UNIVERSE_COLUMNS:
        raise ValueError("EMA universe columns do not match the release schema")
    if frame.empty:
        raise ValueError("EMA universe release must not be empty")
    decisions = _utc_series_strict(frame["decision_time"], field="decision_time")
    effective_from = _utc_series_strict(
        frame["effective_from"], field="effective_from"
    )
    effective_to = _utc_series_strict(frame["effective_to"], field="effective_to")
    available = _utc_series_strict(frame["available_at"], field="available_at")
    window_end = _utc_series_strict(
        frame["trailing_window_end"], field="trailing_window_end"
    )
    if not decisions.equals(effective_from):
        raise ValueError("decision_time must equal effective_from")
    if (effective_to <= effective_from).any():
        raise ValueError("effective intervals must be non-empty half-open ranges")
    interval_rows = pd.DataFrame(
        {"effective_from": effective_from, "effective_to": effective_to}
    ).drop_duplicates()
    if interval_rows["effective_from"].duplicated().any():
        raise ValueError("a monthly snapshot has ambiguous effective_to boundaries")
    interval_rows = interval_rows.sort_values("effective_from").reset_index(drop=True)
    if len(interval_rows) > 1 and not (
        interval_rows["effective_to"].iloc[:-1].reset_index(drop=True)
        == interval_rows["effective_from"].iloc[1:].reset_index(drop=True)
    ).all():
        raise ValueError("universe effective intervals must be contiguous and non-overlapping")
    for row in interval_rows.itertuples(index=False):
        if row.effective_to != _next_month_start(row.effective_from):
            raise ValueError("universe release contains a missing monthly snapshot")
    if (available > effective_from).any():
        raise ValueError("universe release contains future available_at")
    if (window_end > available).any():
        raise ValueError("universe release contains a non-causal trailing window")
    symbols = frame["symbol"]
    if symbols.isna().any() or symbols.astype(str).str.len().eq(0).any():
        raise ValueError("universe release contains an empty symbol")
    if pd.DataFrame({"decision_time": decisions, "symbol": symbols}).duplicated().any():
        raise ValueError("universe release contains duplicate decision_time/symbol rows")
    if frame["eligible"].isna().any() or not frame["eligible"].map(
        lambda value: isinstance(value, bool)
    ).all():
        raise ValueError("eligible must contain only booleans")
    reasons = frame["missing_reason"]
    if reasons[frame["eligible"]].notna().any():
        raise ValueError("eligible symbols must not have missing_reason")
    if reasons[~frame["eligible"]].isna().any() or reasons[
        ~frame["eligible"]
    ].astype(str).str.len().eq(0).any():
        raise ValueError("ineligible symbols must have missing_reason")
    ranks = pd.to_numeric(frame["rank"], errors="coerce")
    if ranks[frame["eligible"]].isna().any() or ranks[~frame["eligible"]].notna().any():
        raise ValueError("rank must be present only for eligible symbols")
    for _, group in frame.loc[frame["eligible"]].groupby(decisions[frame["eligible"]]):
        expected = list(range(1, len(group) + 1))
        if sorted(pd.to_numeric(group["rank"]).astype(int).tolist()) != expected:
            raise ValueError("eligible ranks must be unique and contiguous")
    months = {(value.year, value.month) for value in decisions.unique()}
    if len(months) != len(decisions.unique()):
        raise ValueError("universe release contains multiple decisions in one UTC month")
    for column in ("listing_source_id", "snapshot_source_id", "rule_source_id"):
        if not frame[column].map(_is_source_id).all():
            raise ValueError(f"invalid content-addressed source identifier: {column}")


def _canonical_universe_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    _validate_universe_frame(frame)
    canonical = frame.loc[:, UNIVERSE_COLUMNS].copy()
    canonical["decision_time"] = _utc_series_strict(
        canonical["decision_time"], field="decision_time"
    )
    canonical["effective_from"] = _utc_series_strict(
        canonical["effective_from"], field="effective_from"
    )
    canonical["effective_to"] = _utc_series_strict(
        canonical["effective_to"], field="effective_to"
    )
    canonical["available_at"] = _utc_series_strict(
        canonical["available_at"], field="available_at"
    )
    canonical["trailing_window_end"] = _utc_series_strict(
        canonical["trailing_window_end"], field="trailing_window_end"
    )
    canonical = canonical.sort_values(["decision_time", "symbol"])
    records: list[dict[str, Any]] = []
    for row in canonical.to_dict(orient="records"):
        records.append({key: _canonical_scalar(value) for key, value in row.items()})
    return records


def _next_month_start(value: pd.Timestamp) -> pd.Timestamp:
    if value != value.normalize() or value.day != 1:
        raise ValueError("monthly decision_time must be UTC midnight on the first day")
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return pd.Timestamp(year=year, month=month, day=1, tz="UTC")


def _is_source_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _aware_utc(value: Any, *, field: str) -> pd.Timestamp:
    if value is None:
        raise ValueError(f"{field} must be timezone-aware")
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _utc_series_strict(values: pd.Series, *, field: str) -> pd.Series:
    converted: list[pd.Timestamp] = []
    for value in values:
        try:
            converted.append(_aware_utc(value, field=field))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} contains an invalid timestamp") from exc
    return pd.Series(pd.DatetimeIndex(converted), index=values.index, name=values.name)


def _canonical_scalar(value: Any) -> Any:
    if value is pd.NA or value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return _aware_utc(value, field="timestamp").isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if not math.isfinite(value):
            raise ValueError("universe content contains a non-finite number")
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return _canonical_scalar(value.item())
    raise TypeError(f"unsupported universe value type: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


__all__ = [
    "EMA_UNIVERSE_SCHEMA",
    "UNIVERSE_COLUMNS",
    "ema_universe_content_hash",
    "_aware_utc",
    "_canonical_json",
    "_canonical_scalar",
    "_canonical_universe_records",
    "_is_source_id",
    "_next_month_start",
    "_utc_series_strict",
    "_validate_universe_frame",
]
