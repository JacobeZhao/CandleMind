"""Reproducible point-in-time universe releases for EMA research."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import math
from typing import Any, Iterable, Mapping

import pandas as pd

from backend.app.services.pit_universe_contract import (
    EMA_UNIVERSE_SCHEMA,
    UNIVERSE_COLUMNS,
    _aware_utc,
    _canonical_json,
    _next_month_start,
    _utc_series_strict,
    _validate_universe_frame,
    ema_universe_content_hash,
)
from backend.app.services.point_in_time_universe import (
    ContractListing,
    MINIMUM_SEASONING,
    MonthlySnapshot,
    select_eligible_contracts,
)


_ALIGNMENT_COLUMNS = ("observation_available", "tradable", "missing_reason")


def build_ema_universe_snapshots(
    listings: Iterable[ContractListing],
    snapshots: Iterable[MonthlySnapshot],
    *,
    decision_times: Iterable[datetime | pd.Timestamp],
    release_end: datetime | pd.Timestamp | None = None,
    minimum_quote_volume: float | None = None,
    top_n: int | None = None,
    minimum_seasoning: timedelta = MINIMUM_SEASONING,
) -> pd.DataFrame:
    """Build an immutable-shape monthly point-in-time universe table.

    Each monthly snapshot is effective on ``[effective_from, effective_to)``.
    The next snapshot starts the next interval. The final boundary is either
    ``release_end`` or the explicitly materialized next UTC month boundary.
    No future snapshot is ever carried backward.
    """

    decisions = _prepare_decision_times(decision_times)
    intervals = _prepare_effective_intervals(decisions, release_end=release_end)
    listing_records = tuple(listings)
    snapshot_records = tuple(snapshots)
    _validate_rule(
        decisions[0],
        minimum_quote_volume=minimum_quote_volume,
        top_n=top_n,
        minimum_seasoning=minimum_seasoning,
    )
    rule_payload = {
        "minimum_quote_volume": minimum_quote_volume,
        "top_n": top_n,
        "minimum_seasoning_seconds": minimum_seasoning.total_seconds(),
    }
    rule_source_id = _source_id(rule_payload)
    rows: list[dict[str, Any]] = []

    for decision_time, effective_to in intervals:
        matching_snapshots = _snapshots_for_decision(snapshot_records, decision_time)
        if not matching_snapshots:
            raise ValueError(
                f"no monthly snapshots exist for decision_time={decision_time.isoformat()}"
            )
        _validate_unique_records(
            matching_snapshots,
            kind="snapshot",
            decision_time=decision_time,
        )

        causal_listings = _causal_listings(listing_records, decision_time)
        _validate_unique_records(
            causal_listings,
            kind="listing",
            decision_time=decision_time,
        )
        listings_by_symbol = {item.contract_id: item for item in causal_listings}
        eligible = select_eligible_contracts(
            causal_listings,
            matching_snapshots,
            decision_time=decision_time.to_pydatetime(),
            minimum_quote_volume=minimum_quote_volume,
            top_n=top_n,
            minimum_seasoning=minimum_seasoning,
        )
        ranks = {symbol: rank for rank, symbol in enumerate(eligible, start=1)}

        decision_rows = 0
        for snapshot in sorted(matching_snapshots, key=lambda item: item.contract_id):
            listing = listings_by_symbol.get(snapshot.contract_id)
            # A current symbol list must never disclose or backfill a contract
            # before its point-in-time listing record becomes causal.
            if listing is None:
                continue
            decision_rows += 1
            symbol = snapshot.contract_id
            is_eligible = symbol in ranks
            rows.append(
                {
                    "decision_time": decision_time,
                    "effective_from": decision_time,
                    "effective_to": effective_to,
                    "symbol": symbol,
                    "eligible": is_eligible,
                    "rank": ranks.get(symbol),
                    "missing_reason": (
                        None
                        if is_eligible
                        else _ineligible_reason(
                            listing,
                            snapshot,
                            decision_time=decision_time,
                            minimum_quote_volume=minimum_quote_volume,
                            top_n=top_n,
                            minimum_seasoning=minimum_seasoning,
                            eligible_symbols=set(eligible),
                        )
                    ),
                    "available_at": _aware_utc(
                        snapshot.available_at, field="snapshot.available_at"
                    ),
                    "trailing_window_end": _aware_utc(
                        snapshot.trailing_window_end,
                        field="snapshot.trailing_window_end",
                    ),
                    "trailing_quote_volume": snapshot.trailing_quote_volume,
                    "data_complete": snapshot.data_complete,
                    "fee_available": snapshot.fee_available,
                    "funding_available": snapshot.funding_available,
                    "cost_available": snapshot.cost_available,
                    "liquidity_rule": (
                        "minimum_quote_volume"
                        if minimum_quote_volume is not None
                        else "top_n"
                    ),
                    "minimum_quote_volume": minimum_quote_volume,
                    "top_n": top_n,
                    "minimum_seasoning_seconds": minimum_seasoning.total_seconds(),
                    "listing_source_id": _listing_source_id(listing),
                    "snapshot_source_id": _snapshot_source_id(snapshot),
                    "rule_source_id": rule_source_id,
                }
            )
        if decision_rows == 0:
            raise ValueError(
                "monthly snapshots have no causal listing records at "
                f"decision_time={decision_time.isoformat()}"
            )

    frame = pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)
    frame["rank"] = pd.array(frame["rank"], dtype="Int64")
    frame = frame.sort_values(["decision_time", "symbol"]).reset_index(drop=True)
    _validate_universe_frame(frame)
    return frame


def validate_ema_universe_prefix_invariance(
    prefix: pd.DataFrame,
    extended: pd.DataFrame,
) -> str:
    """Verify that adding later months cannot change an existing release prefix."""

    _validate_universe_frame(prefix)
    _validate_universe_frame(extended)
    prefix_times = tuple(sorted(prefix["decision_time"].unique()))
    if not prefix_times:
        raise ValueError("prefix universe must not be empty")
    cutoff = prefix_times[-1]
    extended_prefix = extended.loc[extended["decision_time"] <= cutoff, UNIVERSE_COLUMNS]
    extended_times = tuple(sorted(extended_prefix["decision_time"].unique()))
    if extended_times != prefix_times:
        raise ValueError("extended release does not contain the same decision-time prefix")
    expected = ema_universe_content_hash(prefix)
    actual = ema_universe_content_hash(extended_prefix)
    if actual != expected:
        raise ValueError("EMA universe prefix invariance check failed")
    return expected


def align_ema_observations_to_universe(
    universe_snapshots: pd.DataFrame,
    observations: pd.DataFrame,
    *,
    decision_time: datetime | pd.Timestamp,
    observation_time_column: str = "observation_available_at",
) -> pd.DataFrame:
    """As-of align observations to every symbol in the effective membership.

    Ineligible symbols remain visible. An observation is usable only when its
    availability timestamp is no later than the requested 5-minute decision.
    """

    _validate_universe_frame(universe_snapshots)
    requested = _five_minute_utc(decision_time, field="decision_time")
    universe = ema_universe_as_of(universe_snapshots, decision_time=requested)

    required = {"symbol", observation_time_column}
    missing_columns = required - set(observations.columns)
    if missing_columns:
        raise ValueError(f"observations missing required columns: {sorted(missing_columns)}")
    reserved = set(_ALIGNMENT_COLUMNS) & set(observations.columns)
    if reserved:
        raise ValueError(f"observations use reserved alignment columns: {sorted(reserved)}")

    observation_times = _utc_series_strict(
        observations[observation_time_column], field=observation_time_column
    )
    panel = observations.copy()
    panel[observation_time_column] = observation_times
    if panel["symbol"].isna().any() or panel["symbol"].astype(str).str.len().eq(0).any():
        raise ValueError("observations contain an empty symbol")
    panel["symbol"] = panel["symbol"].astype(str)
    if panel.duplicated(["symbol", observation_time_column]).any():
        raise ValueError("observations contain ambiguous duplicate symbol/timestamp rows")

    known_symbols = set(panel["symbol"])
    panel = panel.loc[panel[observation_time_column] <= requested]
    panel = panel.sort_values(["symbol", observation_time_column])
    panel = panel.groupby("symbol", as_index=False, sort=False).tail(1)

    if universe.empty:
        result = universe.copy()
        for column in observations.columns:
            if column not in result.columns:
                result[column] = pd.Series(dtype=observations[column].dtype)
        result["observation_available"] = pd.Series(dtype=bool)
        result["tradable"] = pd.Series(dtype=bool)
        result["missing_reason"] = pd.Series(dtype="object")
        return result

    feature_columns = [
        column
        for column in panel.columns
        if column not in set(UNIVERSE_COLUMNS) - {"symbol"}
    ]
    panel = panel.loc[:, feature_columns]
    panel["_observation_present"] = True
    result = universe.merge(panel, on="symbol", how="left", validate="one_to_one")
    result["observation_available"] = result["_observation_present"].eq(True)
    result["tradable"] = result["eligible"] & result["observation_available"]
    eligible_without_observation = result["eligible"] & ~result["observation_available"]
    delayed = eligible_without_observation & result["symbol"].isin(known_symbols)
    result.loc[eligible_without_observation & ~delayed, "missing_reason"] = "observation_missing"
    result.loc[delayed, "missing_reason"] = "observation_not_yet_available"
    return result.drop(columns="_observation_present")


def ema_universe_as_of(
    universe_snapshots: pd.DataFrame,
    *,
    decision_time: datetime | pd.Timestamp,
) -> pd.DataFrame:
    """Map an arbitrary 5-minute decision to its effective monthly membership."""

    _validate_universe_frame(universe_snapshots)
    requested = _five_minute_utc(decision_time, field="decision_time")
    effective_from = _utc_series_strict(
        universe_snapshots["effective_from"], field="effective_from"
    )
    effective_to = _utc_series_strict(
        universe_snapshots["effective_to"], field="effective_to"
    )
    mask = (effective_from <= requested) & (requested < effective_to)
    result = universe_snapshots.loc[mask].copy()
    if result.empty:
        raise ValueError("universe release has no effective interval for decision_time")
    interval_count = result[["effective_from", "effective_to"]].drop_duplicates()
    if len(interval_count) != 1:
        raise ValueError("universe release has overlapping effective intervals")
    result["decision_time"] = requested
    return result.sort_values(
        ["eligible", "rank", "symbol"], ascending=[False, True, True], na_position="last"
    ).reset_index(drop=True)


# A descriptive alias for callers that treat the operation as a mapping step.
map_tradable_universe_to_observations = align_ema_observations_to_universe


def _prepare_decision_times(
    values: Iterable[datetime | pd.Timestamp],
) -> tuple[pd.Timestamp, ...]:
    decisions = tuple(_aware_utc(value, field="decision_time") for value in values)
    if not decisions:
        raise ValueError("decision_times must not be empty")
    if len(set(decisions)) != len(decisions):
        raise ValueError("decision_times must be unique")
    months = [(value.year, value.month) for value in decisions]
    if len(set(months)) != len(months):
        raise ValueError("decision_times must contain at most one decision per UTC month")
    return tuple(sorted(decisions))


def _prepare_effective_intervals(
    decisions: tuple[pd.Timestamp, ...],
    *,
    release_end: datetime | pd.Timestamp | None,
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    final_boundary = (
        _next_month_start(decisions[-1])
        if release_end is None
        else _aware_utc(release_end, field="release_end")
    )
    boundaries = (*decisions, final_boundary)
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        if end <= start:
            raise ValueError("universe effective boundaries must be strictly increasing")
        expected = _next_month_start(start)
        if end != expected:
            label = "release_end" if index == len(decisions) - 1 else "decision_times"
            raise ValueError(
                f"{label} reveals a missing monthly snapshot: expected {expected.isoformat()}"
            )
    return tuple(zip(decisions, boundaries[1:]))


def _ineligible_reason(
    listing: ContractListing,
    snapshot: MonthlySnapshot,
    *,
    decision_time: pd.Timestamp,
    minimum_quote_volume: float | None,
    top_n: int | None,
    minimum_seasoning: timedelta,
    eligible_symbols: set[str],
) -> str:
    listed_at = _aware_utc(listing.listed_at, field="listing.listed_at")
    if listing.final_tradable_at is not None:
        final_tradable_at = _aware_utc(
            listing.final_tradable_at, field="listing.final_tradable_at"
        )
        if final_tradable_at < listed_at:
            return "invalid_listing_window"
        if final_tradable_at < decision_time:
            return "contract_no_longer_tradable"
    if decision_time - listed_at < minimum_seasoning:
        return "minimum_seasoning_not_met"
    for field, reason in (
        ("data_complete", "data_incomplete"),
        ("fee_available", "fee_unavailable"),
        ("funding_available", "funding_unavailable"),
        ("cost_available", "cost_unavailable"),
    ):
        if getattr(snapshot, field) is not True:
            return reason
    volume = snapshot.trailing_quote_volume
    if isinstance(volume, bool) or volume is None:
        return "liquidity_unavailable"
    try:
        valid_volume = math.isfinite(volume) and volume >= 0
    except TypeError:
        valid_volume = False
    if not valid_volume:
        return "liquidity_unavailable"
    if minimum_quote_volume is not None and volume < minimum_quote_volume:
        return "below_minimum_quote_volume"
    if top_n is not None and listing.contract_id not in eligible_symbols:
        return "outside_top_n"
    return "eligibility_requirements_not_met"


def _validate_rule(
    decision_time: pd.Timestamp,
    *,
    minimum_quote_volume: float | None,
    top_n: int | None,
    minimum_seasoning: timedelta,
) -> None:
    # Reuse the authoritative selector's configuration contract.
    select_eligible_contracts(
        (),
        (),
        decision_time=decision_time.to_pydatetime(),
        minimum_quote_volume=minimum_quote_volume,
        top_n=top_n,
        minimum_seasoning=minimum_seasoning,
    )


def _snapshots_for_decision(
    snapshots: tuple[MonthlySnapshot, ...],
    decision_time: pd.Timestamp,
) -> tuple[MonthlySnapshot, ...]:
    matching: list[MonthlySnapshot] = []
    for snapshot in snapshots:
        snapshot_decision = _aware_utc(
            snapshot.decision_time, field="snapshot.decision_time"
        )
        if snapshot_decision != decision_time:
            continue
        available = _aware_utc(snapshot.available_at, field="snapshot.available_at")
        window_end = _aware_utc(
            snapshot.trailing_window_end, field="snapshot.trailing_window_end"
        )
        if available > decision_time:
            raise ValueError("snapshot.available_at cannot be after decision_time")
        if window_end > available:
            raise ValueError("snapshot trailing window cannot end after available_at")
        matching.append(snapshot)
    return tuple(matching)


def _causal_listings(
    listings: tuple[ContractListing, ...], decision_time: pd.Timestamp
) -> tuple[ContractListing, ...]:
    causal: list[ContractListing] = []
    for listing in listings:
        if not isinstance(listing.contract_id, str) or not listing.contract_id:
            raise ValueError("listing contract_id must be a non-empty string")
        listed_at = _aware_utc(listing.listed_at, field="listing.listed_at")
        if listed_at <= decision_time:
            causal.append(listing)
    return tuple(causal)


def _validate_unique_records(
    records: Iterable[ContractListing | MonthlySnapshot],
    *,
    kind: str,
    decision_time: pd.Timestamp,
) -> None:
    seen: set[str] = set()
    for record in records:
        symbol = record.contract_id
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"{kind} contract_id must be a non-empty string")
        if symbol in seen:
            raise ValueError(
                f"ambiguous duplicate {kind} for {symbol} at {decision_time.isoformat()}"
            )
        seen.add(symbol)


def _listing_source_id(listing: ContractListing) -> str:
    return _source_id(
        {
            "record_type": "ContractListing",
            "contract_id": listing.contract_id,
            "listed_at": _aware_utc(
                listing.listed_at, field="listing.listed_at"
            ).isoformat(),
            "final_tradable_at": (
                None
                if listing.final_tradable_at is None
                else _aware_utc(
                    listing.final_tradable_at, field="listing.final_tradable_at"
                ).isoformat()
            ),
        }
    )


def _snapshot_source_id(snapshot: MonthlySnapshot) -> str:
    return _source_id(
        {
            "record_type": "MonthlySnapshot",
            "contract_id": snapshot.contract_id,
            "decision_time": _aware_utc(
                snapshot.decision_time, field="snapshot.decision_time"
            ).isoformat(),
            "available_at": _aware_utc(
                snapshot.available_at, field="snapshot.available_at"
            ).isoformat(),
            "trailing_window_end": _aware_utc(
                snapshot.trailing_window_end, field="snapshot.trailing_window_end"
            ).isoformat(),
            "trailing_quote_volume": snapshot.trailing_quote_volume,
            "data_complete": snapshot.data_complete,
            "fee_available": snapshot.fee_available,
            "funding_available": snapshot.funding_available,
            "cost_available": snapshot.cost_available,
        }
    )


def _source_id(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _five_minute_utc(value: Any, *, field: str) -> pd.Timestamp:
    timestamp = _aware_utc(value, field=field)
    if timestamp.second != 0 or timestamp.microsecond != 0 or timestamp.minute % 5 != 0:
        raise ValueError(f"{field} must lie on the UTC 5-minute grid")
    return timestamp


__all__ = [
    "EMA_UNIVERSE_SCHEMA",
    "UNIVERSE_COLUMNS",
    "align_ema_observations_to_universe",
    "build_ema_universe_snapshots",
    "ema_universe_content_hash",
    "ema_universe_as_of",
    "map_tradable_universe_to_observations",
    "validate_ema_universe_prefix_invariance",
]
