from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from backend.app.services.pit_universe_contract import (
    EMA_UNIVERSE_SCHEMA,
    UNIVERSE_COLUMNS,
    ema_universe_content_hash,
)
from backend.app.services.ema_data_release import (
    EmaDataReleaseError,
    build_ema_data_release,
    dataframe_semantic_sha256,
    verify_ema_data_release,
)
from backend.tests.ema_release_support import write_pit_evidence


REVISION = "1" * 40
DECISION = pd.Timestamp("2025-01-01T00:00:00Z")
WARMUP_DAYS = 1
LABEL_HORIZON_DAYS = 1
UNIVERSE_GOLDEN_SHA256 = (
    "97faf9c2f5e45c2b41dc8b2f0372ecefa1594572e44901932bf59bbed849af79"
)


def _bars(
    *,
    duplicate: bool = False,
    start: pd.Timestamp | None = None,
    periods: int = 576,
) -> pd.DataFrame:
    times = pd.date_range(
        start or DECISION - pd.Timedelta(days=WARMUP_DAYS),
        periods=periods,
        freq="5min",
        tz="UTC",
    )
    if duplicate:
        times = pd.DatetimeIndex([times[0], times[0], *times[2:]])
    opens = [100.0 + index * 0.01 for index in range(len(times))]
    return pd.DataFrame(
        {
            "open_time": times,
            "open": opens,
            "high": [value + 1.0 for value in opens],
            "low": [value - 1.0 for value in opens],
            "close": [value + 0.5 for value in opens],
            "volume": [10.0 + index for index in range(len(times))],
        }
    )


def _universe(*, future_available: bool = False, duplicate: bool = False) -> pd.DataFrame:
    decision = DECISION
    available = decision + pd.Timedelta(minutes=1) if future_available else decision
    frame = pd.DataFrame(
        [{
            "decision_time": decision,
            "effective_from": decision,
            "effective_to": pd.Timestamp("2025-02-01T00:00:00Z"),
            "symbol": "BTCUSDT",
            "eligible": True,
            "rank": 1,
            "missing_reason": None,
            "available_at": available,
            "trailing_window_end": decision - pd.Timedelta(days=1),
            "trailing_quote_volume": 1_000_000.0,
            "data_complete": True,
            "fee_available": True,
            "funding_available": True,
            "cost_available": True,
            "liquidity_rule": "top_n",
            "minimum_quote_volume": None,
            "top_n": 30,
            "minimum_seasoning_seconds": 2_592_000.0,
            "listing_source_id": "sha256:" + "1" * 64,
            "snapshot_source_id": "sha256:" + "2" * 64,
            "rule_source_id": "sha256:" + "3" * 64,
        }],
        columns=UNIVERSE_COLUMNS,
    )
    frame["rank"] = pd.array(frame["rank"], dtype="Int64")
    return pd.concat([frame, frame], ignore_index=True) if duplicate else frame


def _sources(tmp_path: Path, **universe_options: bool) -> tuple[Path, Path]:
    bars = tmp_path / "BTCUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    bar_frame = _bars()
    bar_frame["open_time"] = bar_frame["open_time"].astype("int64") // 1_000_000
    bar_frame.to_parquet(bars, index=False)
    _universe(**universe_options).to_parquet(universe, index=False)
    return bars, universe


def _build(tmp_path: Path, bars: Path, universe: Path, *, release_id: str = "r1"):
    universe_manifest, readiness = write_pit_evidence(tmp_path, universe)
    return build_ema_data_release(
        release_id=release_id,
        output_root=tmp_path / "releases",
        ohlc_paths=[bars],
        universe_snapshots_path=universe,
        universe_manifest_path=universe_manifest,
        pit_readiness_path=readiness,
        warmup_days=WARMUP_DAYS,
        label_horizon_days=LABEL_HORIZON_DAYS,
        code_revision=REVISION,
    )


def test_universe_content_hash_matches_golden_vector() -> None:
    assert ema_universe_content_hash(_universe()) == UNIVERSE_GOLDEN_SHA256


def test_universe_content_hash_is_independent_of_row_order() -> None:
    frame = pd.concat(
        [_universe(), _universe().assign(symbol="ETHUSDT", rank=2)],
        ignore_index=True,
    )
    reversed_frame = frame.iloc[::-1].reset_index(drop=True)

    assert ema_universe_content_hash(frame) == ema_universe_content_hash(
        reversed_frame
    )


def test_builds_atomic_release_with_complete_evidence(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path)
    manifest = _build(tmp_path, bars, universe)
    release = tmp_path / "releases" / "r1"

    assert manifest["code_revision"] == REVISION
    assert manifest["window"] == {
        "start": "2024-12-31T00:00:00+00:00",
        "end": "2025-01-02T00:00:00+00:00",
        "semantics": "[start,end)",
    }
    assert manifest["coverage"] == {
        "warmup_days": 1,
        "label_horizon_days": 1,
        "bar_interval": "5min",
        "required_window_semantics": "[decision-warmup,decision+label_horizon)",
        "eligible_pair_count": 1,
    }
    assert len(manifest["release_digest"]) == 64
    assert manifest["universe"] == {
        "schema": EMA_UNIVERSE_SCHEMA,
        "content_sha256": ema_universe_content_hash(_universe()),
        "interval_semantics": "[effective_from,effective_to)",
    }
    assert manifest["pit_evidence"]["pit_readiness_audit"]["status"] == "ready"
    assert {
        item["path"] for item in manifest["pit_evidence"].values()
    } == {
        "evidence/pit_universe_manifest.json",
        "evidence/pit_readiness_audit.json",
    }
    assert len(manifest["source_snapshot"]["source_tree_sha256"]) == 64
    assert {
        item["repository_path"] for item in manifest["source_snapshot"]["files"]
    } == {
        "backend/app/services/ema_data_release.py",
        "backend/scripts/data/build_ema_data_release.py",
        "backend/app/rl/ema_universe.py",
        "backend/app/services/point_in_time_universe.py",
        "backend/app/rl/ema_features_v2.py",
        "backend/app/rl/ema_lifecycle.py",
    }
    assert {item["kind"] for item in manifest["inputs"]} == {
        "ohlcv",
        "point_in_time_universe",
    }
    assert {item["path"] for item in manifest["outputs"]} == {
        "ohlcv/BTCUSDT_5m.parquet",
        "universe_snapshots.parquet",
    }
    for item in [*manifest["inputs"], *manifest["outputs"]]:
        assert item["rows"] > 0
        assert len(item["sha256"]) == 64
        assert len(item["semantic_sha256"]) == 64
        assert len(item["schema"]["sha256"]) == 64
        assert item["window"]["semantics"] == "[start,end)"
    assert verify_ema_data_release(release) == manifest


def test_release_window_encloses_different_symbol_histories(tmp_path: Path) -> None:
    btc = tmp_path / "BTCUSDT_5m.parquet"
    eth = tmp_path / "ETHUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    _bars().to_parquet(btc, index=False)
    later = _bars(periods=577)
    later.to_parquet(eth, index=False)
    snapshots = pd.concat(
        [_universe(), _universe().assign(symbol="ETHUSDT", rank=2)],
        ignore_index=True,
    )
    snapshots.to_parquet(universe, index=False)
    universe_manifest, readiness = write_pit_evidence(tmp_path, universe)

    manifest = build_ema_data_release(
        release_id="multi",
        output_root=tmp_path / "releases",
        ohlc_paths=[btc, eth],
        universe_snapshots_path=universe,
        universe_manifest_path=universe_manifest,
        pit_readiness_path=readiness,
        warmup_days=WARMUP_DAYS,
        label_horizon_days=LABEL_HORIZON_DAYS,
        code_revision=REVISION,
    )

    assert manifest["window"] == {
        "start": "2024-12-31T00:00:00+00:00",
        "end": "2025-01-02T00:05:00+00:00",
        "semantics": "[start,end)",
    }


def test_verifier_rejects_byte_tampering(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path)
    _build(tmp_path, bars, universe)
    output = tmp_path / "releases" / "r1" / "ohlcv" / bars.name
    with output.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(EmaDataReleaseError, match="byte hash"):
        verify_ema_data_release(tmp_path / "releases" / "r1")


def test_verifier_rejects_semantic_tampering_even_with_rehashed_bytes(
    tmp_path: Path,
) -> None:
    bars, universe = _sources(tmp_path)
    _build(tmp_path, bars, universe)
    release = tmp_path / "releases" / "r1"
    output = release / "ohlcv" / bars.name
    changed = pd.read_parquet(output)
    changed.loc[0, "close"] = 100.75
    changed.to_parquet(output, index=False)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    record = next(item for item in manifest["outputs"] if item["kind"] == "ohlcv")
    changed_bytes = output.stat().st_size
    changed_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    record["bytes"] = changed_bytes
    record["sha256"] = changed_sha256
    source_record = next(
        item for item in manifest["inputs"] if item["source_id"] == record["source_id"]
    )
    source_record["bytes"] = changed_bytes
    source_record["sha256"] = changed_sha256
    from backend.app.services.ema_data_release import (
        _source_record_id,
        canonical_manifest_sha256,
        canonical_release_digest,
    )

    updated_source_id = _source_record_id(source_record)
    source_record["source_id"] = updated_source_id
    record["source_id"] = updated_source_id
    manifest["release_digest"] = canonical_release_digest(manifest)
    manifest["manifest_sha256"] = canonical_manifest_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="ascii")

    with pytest.raises(EmaDataReleaseError, match="semantic hash"):
        verify_ema_data_release(release)


@pytest.mark.parametrize("duplicate_kind", ["bars", "universe"])
def test_rejects_duplicate_records(tmp_path: Path, duplicate_kind: str) -> None:
    bars = tmp_path / "BTCUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    _bars(duplicate=duplicate_kind == "bars").to_parquet(bars, index=False)
    _universe(duplicate=duplicate_kind == "universe").to_parquet(universe, index=False)

    with pytest.raises(EmaDataReleaseError, match="duplicate"):
        _build(tmp_path, bars, universe)


def test_rejects_future_available_at(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path, future_available=True)

    with pytest.raises(EmaDataReleaseError, match="future available_at"):
        _build(tmp_path, bars, universe)


def test_rejects_noncanonical_universe_without_effective_intervals(
    tmp_path: Path,
) -> None:
    bars = tmp_path / "BTCUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    _bars().to_parquet(bars, index=False)
    _universe().drop(columns=["effective_from", "effective_to"]).to_parquet(
        universe, index=False
    )

    with pytest.raises(EmaDataReleaseError, match="columns do not match"):
        _build(tmp_path, bars, universe)


def test_verifier_rejects_forged_universe_content_binding(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path)
    _build(tmp_path, bars, universe)
    release = tmp_path / "releases" / "r1"
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["universe"]["content_sha256"] = "f" * 64
    from backend.app.services.ema_data_release import (
        canonical_manifest_sha256,
        canonical_release_digest,
    )

    manifest["release_digest"] = canonical_release_digest(manifest)
    manifest["manifest_sha256"] = canonical_manifest_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="ascii")

    with pytest.raises(EmaDataReleaseError, match="universe content hash"):
        verify_ema_data_release(release)


def test_refuses_existing_release_without_touching_it(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path)
    release = tmp_path / "releases" / "r1"
    release.mkdir(parents=True)
    sentinel = release / "keep.txt"
    sentinel.write_text("keep", encoding="ascii")

    with pytest.raises(FileExistsError, match="already exists"):
        _build(tmp_path, bars, universe)

    assert sentinel.read_text(encoding="ascii") == "keep"


def test_semantic_hash_is_independent_of_parquet_encoding(tmp_path: Path) -> None:
    frame = _bars()
    zstd = tmp_path / "zstd.parquet"
    gzip = tmp_path / "gzip.parquet"
    frame.to_parquet(zstd, index=False, compression="zstd")
    frame.to_parquet(gzip, index=False, compression="gzip")

    assert zstd.read_bytes() != gzip.read_bytes()
    assert dataframe_semantic_sha256(pd.read_parquet(zstd)) == dataframe_semantic_sha256(
        pd.read_parquet(gzip)
    )


def test_source_id_is_content_addressed_not_path_addressed(tmp_path: Path) -> None:
    first = tmp_path / "first" / "BTCUSDT_5m.parquet"
    second = tmp_path / "second" / "BTCUSDT_5m.parquet"
    first.parent.mkdir()
    second.parent.mkdir()
    _bars().to_parquet(first, index=False)
    _bars().to_parquet(second, index=False)
    universe = tmp_path / "universe.parquet"
    _universe().to_parquet(universe, index=False)
    universe_manifest, readiness = write_pit_evidence(tmp_path, universe)
    common = {
        "output_root": tmp_path / "releases",
        "universe_snapshots_path": universe,
        "universe_manifest_path": universe_manifest,
        "pit_readiness_path": readiness,
        "warmup_days": WARMUP_DAYS,
        "label_horizon_days": LABEL_HORIZON_DAYS,
        "code_revision": REVISION,
    }
    first_manifest = build_ema_data_release(
        release_id="first", ohlc_paths=[first], **common
    )
    second_manifest = build_ema_data_release(
        release_id="second", ohlc_paths=[second], **common
    )
    first_sources = {item["kind"]: item["source_id"] for item in first_manifest["inputs"]}
    second_sources = {item["kind"]: item["source_id"] for item in second_manifest["inputs"]}

    assert first_sources == second_sources
    assert first_manifest["release_digest"] == second_manifest["release_digest"]


def test_rejects_current_pointer_and_duplicate_source_path(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    bars = current / "BTCUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    _bars().to_parquet(bars, index=False)
    _universe().to_parquet(universe, index=False)

    with pytest.raises(EmaDataReleaseError, match="current pointer"):
        _build(tmp_path, bars, universe)

    explicit = tmp_path / "BTCUSDT_5m.parquet"
    _bars().to_parquet(explicit, index=False)
    universe_manifest, readiness = write_pit_evidence(tmp_path, universe)
    with pytest.raises(EmaDataReleaseError, match="distinct"):
        build_ema_data_release(
            release_id="r2",
            output_root=tmp_path / "releases",
            ohlc_paths=[explicit, explicit],
            universe_snapshots_path=universe,
            universe_manifest_path=universe_manifest,
            pit_readiness_path=readiness,
            warmup_days=WARMUP_DAYS,
            label_horizon_days=LABEL_HORIZON_DAYS,
            code_revision=REVISION,
        )


def test_rejects_eligible_symbol_without_ohlc(tmp_path: Path) -> None:
    bars = tmp_path / "BTCUSDT_5m.parquet"
    universe = tmp_path / "universe.parquet"
    _bars().to_parquet(bars, index=False)
    snapshots = pd.concat(
        [_universe(), _universe().assign(symbol="ETHUSDT", rank=2)],
        ignore_index=True,
    )
    snapshots.to_parquet(universe, index=False)

    with pytest.raises(EmaDataReleaseError, match="eligible.*lack OHLC"):
        _build(tmp_path, bars, universe)


@pytest.mark.parametrize(
    ("bars", "message"),
    [
        (_bars().iloc[1:].reset_index(drop=True), "warmup coverage"),
        (_bars().iloc[:-1].reset_index(drop=True), "label horizon coverage"),
    ],
)
def test_rejects_insufficient_eligible_coverage(
    tmp_path: Path, bars: pd.DataFrame, message: str
) -> None:
    bars_path = tmp_path / "BTCUSDT_5m.parquet"
    universe_path = tmp_path / "universe.parquet"
    bars.to_parquet(bars_path, index=False)
    _universe().to_parquet(universe_path, index=False)

    with pytest.raises(EmaDataReleaseError, match=message):
        _build(tmp_path, bars_path, universe_path)


def test_verifier_rejects_source_snapshot_tampering(tmp_path: Path) -> None:
    bars, universe = _sources(tmp_path)
    _build(tmp_path, bars, universe)
    release = tmp_path / "releases" / "r1"
    snapshot = release / "source" / "backend" / "app" / "rl" / "ema_lifecycle.py"
    with snapshot.open("ab") as handle:
        handle.write(b"\n# tampered\n")

    with pytest.raises(EmaDataReleaseError, match="source snapshot byte hash"):
        verify_ema_data_release(release)
