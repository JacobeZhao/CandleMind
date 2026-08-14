"""Pure point-in-time contract universe selection.

The selector consumes immutable records and has no database, clock, or filesystem
dependencies. A monthly snapshot is usable only for its stated decision time and
only when every value in it was available by that time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


MINIMUM_SEASONING = timedelta(days=180)


@dataclass(frozen=True)
class ContractListing:
    """Point-in-time listing boundaries for one tradable contract.

    ``final_tradable_at`` is inclusive. ``None`` means that no final trading
    timestamp applies to the contract.
    """

    contract_id: str
    listed_at: datetime | None
    final_tradable_at: datetime | None = None


@dataclass(frozen=True)
class MonthlySnapshot:
    """Liquidity and input-availability state for one monthly decision.

    Optional fields deliberately represent unknown state. Unknown values are
    never imputed by :func:`select_eligible_contracts`; they make the contract
    ineligible for that decision.
    """

    contract_id: str
    decision_time: datetime | None
    available_at: datetime | None
    trailing_window_end: datetime | None
    trailing_quote_volume: float | None
    data_complete: bool | None
    fee_available: bool | None
    funding_available: bool | None
    cost_available: bool | None


def select_eligible_contracts(
    listings: Iterable[ContractListing],
    snapshots: Iterable[MonthlySnapshot],
    *,
    decision_time: datetime,
    minimum_quote_volume: float | None = None,
    top_n: int | None = None,
    minimum_seasoning: timedelta = MINIMUM_SEASONING,
) -> tuple[str, ...]:
    """Return the deterministic eligible universe at ``decision_time``.

    Exactly one liquidity rule must be selected. Threshold selection is
    inclusive; top-N selection is performed after all non-liquidity gates.
    Results are ordered by descending trailing quote volume and then ascending
    contract ID, so equal-volume selections are reproducible.

    Missing or ambiguous contract state fails closed for that contract. Invalid
    selector configuration raises ``ValueError`` because no safe interpretation
    exists for the requested universe.
    """

    _validate_configuration(
        decision_time=decision_time,
        minimum_quote_volume=minimum_quote_volume,
        top_n=top_n,
        minimum_seasoning=minimum_seasoning,
    )

    listings_by_id: dict[str, list[ContractListing]] = {}
    for listing in listings:
        if _listing_is_causal(listing, decision_time):
            listings_by_id.setdefault(listing.contract_id, []).append(listing)

    snapshots_by_id: dict[str, list[MonthlySnapshot]] = {}
    for snapshot in snapshots:
        if _snapshot_is_causal(snapshot, decision_time):
            snapshots_by_id.setdefault(snapshot.contract_id, []).append(snapshot)

    candidates: list[tuple[str, float]] = []
    for contract_id, contract_listings in listings_by_id.items():
        # Conflicting duplicate state is not guessed or resolved by input order.
        if not contract_id or len(contract_listings) != 1:
            continue
        contract_snapshots = snapshots_by_id.get(contract_id)
        if contract_snapshots is None or len(contract_snapshots) != 1:
            continue

        listing = contract_listings[0]
        snapshot = contract_snapshots[0]
        if not _listing_is_eligible(
            listing,
            decision_time=decision_time,
            minimum_seasoning=minimum_seasoning,
        ):
            continue
        if not _snapshot_is_complete(snapshot):
            continue

        volume = snapshot.trailing_quote_volume
        if volume is None or isinstance(volume, bool):
            continue
        try:
            finite_volume = math.isfinite(volume)
        except TypeError:
            continue
        if not finite_volume or volume < 0:
            continue
        if minimum_quote_volume is not None and volume < minimum_quote_volume:
            continue
        candidates.append((contract_id, volume))

    ranked = sorted(candidates, key=lambda item: (-item[1], item[0]))
    if top_n is not None:
        ranked = ranked[:top_n]
    return tuple(contract_id for contract_id, _volume in ranked)


def _validate_configuration(
    *,
    decision_time: datetime,
    minimum_quote_volume: float | None,
    top_n: int | None,
    minimum_seasoning: timedelta,
) -> None:
    if not _is_aware(decision_time):
        raise ValueError("decision_time must be timezone-aware")
    if minimum_seasoning < MINIMUM_SEASONING:
        raise ValueError("minimum_seasoning cannot be less than 180 days")
    if (minimum_quote_volume is None) == (top_n is None):
        raise ValueError("choose exactly one of minimum_quote_volume or top_n")
    if minimum_quote_volume is not None:
        if isinstance(minimum_quote_volume, bool):
            raise ValueError("minimum_quote_volume must be a finite non-negative number")
        try:
            valid_threshold = (
                math.isfinite(minimum_quote_volume) and minimum_quote_volume >= 0
            )
        except TypeError:
            valid_threshold = False
        if not valid_threshold:
            raise ValueError("minimum_quote_volume must be a finite non-negative number")
    if top_n is not None and (
        isinstance(top_n, bool) or not isinstance(top_n, int) or top_n <= 0
    ):
        raise ValueError("top_n must be a positive integer")


def _snapshot_is_causal(
    snapshot: MonthlySnapshot,
    decision_time: datetime,
) -> bool:
    if not isinstance(snapshot.contract_id, str) or not snapshot.contract_id:
        return False
    if not all(
        _is_aware(timestamp)
        for timestamp in (
            snapshot.decision_time,
            snapshot.available_at,
            snapshot.trailing_window_end,
        )
    ):
        return False
    return (
        snapshot.decision_time == decision_time
        and snapshot.available_at <= decision_time
        and snapshot.trailing_window_end <= decision_time
        and snapshot.trailing_window_end <= snapshot.available_at
    )


def _listing_is_causal(
    listing: ContractListing,
    decision_time: datetime,
) -> bool:
    return (
        isinstance(listing.contract_id, str)
        and bool(listing.contract_id)
        and _is_aware(listing.listed_at)
        and listing.listed_at <= decision_time
    )


def _listing_is_eligible(
    listing: ContractListing,
    *,
    decision_time: datetime,
    minimum_seasoning: timedelta,
) -> bool:
    if not isinstance(listing.contract_id, str) or not listing.contract_id:
        return False
    if not _is_aware(listing.listed_at):
        return False
    if listing.listed_at > decision_time:
        return False
    if decision_time - listing.listed_at < minimum_seasoning:
        return False
    if listing.final_tradable_at is None:
        return True
    if not _is_aware(listing.final_tradable_at):
        return False
    if listing.final_tradable_at < listing.listed_at:
        return False
    return decision_time <= listing.final_tradable_at


def _snapshot_is_complete(snapshot: MonthlySnapshot) -> bool:
    return all(
        flag is True
        for flag in (
            snapshot.data_complete,
            snapshot.fee_available,
            snapshot.funding_available,
            snapshot.cost_available,
        )
    )


def _is_aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


__all__ = [
    "ContractListing",
    "MINIMUM_SEASONING",
    "MonthlySnapshot",
    "select_eligible_contracts",
]
