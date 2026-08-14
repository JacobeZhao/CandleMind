from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.app.data_layout import REQUIRED_DIRECTORIES
from backend.app.services.derivatives_data_sync import (
    DerivativeArchiveSpec,
    DerivativesDataIntegrityError,
    DownloadResult,
    build_basis,
    build_book_depth,
    build_funding,
    build_open_interest,
    canonical_manifest_sha256,
    download_archive,
    source_archive_specs,
)
from backend.scripts.data import sync_derivatives


def _zip_csv(name: str, header: list[str], rows: list[list]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        lines = [",".join(header)]
        lines.extend(",".join(map(str, row)) for row in rows)
        archive.writestr(name, "\n".join(lines))
    return output.getvalue()


def _price_zip(name: str, *, offset: float = 0.0) -> bytes:
    rows = []
    start = 1_735_689_600_000
    for index in range(288):
        opened = start + index * 300_000
        close = 100.0 + offset + index / 100
        rows.append([
            opened, close - 0.1, close + 0.2, close - 0.2, close, 0,
            opened + 299_999, 0, 300, 0, 0, 0,
        ])
    return _zip_csv(name, [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "count", "taker_buy_volume",
        "taker_buy_quote_volume", "ignore",
    ], rows)


def _metrics_zip(name: str) -> bytes:
    rows = []
    start = pd.Timestamp("2025-01-01", tz="UTC")
    for index in range(288):
        timestamp = (start + pd.Timedelta(minutes=5 * index)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append([timestamp, "BTCUSDT", 10_000 + index, 1_000_000 + index, 1.1, 1.2, 1.3, 0.9])
    return _zip_csv(name, [
        "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    ], rows)


def _funding_zip_at(name: str, timestamps_ms: list[int]) -> bytes:
    return _zip_csv(name, [
        "calc_time", "funding_interval_hours", "last_funding_rate",
    ], [[timestamp_ms, 8, 0.0001] for timestamp_ms in timestamps_ms])


def _funding_zip(name: str) -> bytes:
    start_ms = 1_735_689_600_000
    return _funding_zip_at(name, [
        start_ms + 15,
        start_ms + 8 * 3_600_000 + 15,
        start_ms + 16 * 3_600_000 + 15,
    ])


def _book_depth_zip(name: str) -> bytes:
    rows = []
    start = pd.Timestamp("2025-01-01", tz="UTC")
    for bar in range(288):
        for seconds in (7, 37):
            timestamp = start + pd.Timedelta(minutes=5 * bar, seconds=seconds)
            for band in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
                notional = 1_000_000 + abs(band) * 10_000 + (50_000 if band < 0 else 0)
                rows.append([
                    timestamp.strftime("%Y-%m-%d %H:%M:%S"), band,
                    notional / 100, notional,
                ])
    return _zip_csv(name, ["timestamp", "percentage", "depth", "notional"], rows)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class _Opener:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.calls: list[str] = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        return _Response(self.payloads[url])


def test_source_specs_use_daily_metrics_and_monthly_complete_price_months():
    metrics = source_archive_specs(
        "metrics", "BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 3)
    )
    prices = source_archive_specs(
        "mark_price", "BTCUSDT", start=date(2025, 1, 1), through=date(2025, 2, 2)
    )
    funding = source_archive_specs(
        "funding", "BTCUSDT", start=date(2025, 1, 15), through=date(2025, 2, 2)
    )

    assert [item.period for item in metrics] == ["daily", "daily", "daily"]
    assert [(item.period, item.key) for item in prices] == [
        ("monthly", "2025-01"),
        ("daily", "2025-02-01"),
        ("daily", "2025-02-02"),
    ]
    assert [(item.period, item.key) for item in funding] == [
        ("monthly", "2025-01"),
        ("monthly", "2025-02"),
    ]


def test_download_archive_requires_official_checksum(tmp_path: Path):
    spec = DerivativeArchiveSpec("metrics", "BTCUSDT", "daily", "2025-01-01")
    payload = _metrics_zip("metrics.csv")
    digest = hashlib.sha256(payload).hexdigest()
    opener = _Opener({
        f"{spec.url}.CHECKSUM": f"{digest}  {spec.filename}\n".encode(),
        spec.url: payload,
    })

    first = download_archive(spec, tmp_path, opener_factory=lambda: opener)
    second = download_archive(spec, tmp_path, opener_factory=lambda: opener)

    assert first.status == "downloaded"
    assert second.status == "cached"
    assert first.sha256 == digest
    assert opener.calls.count(spec.url) == 1


def test_normalized_contracts_are_causal_and_complete(tmp_path: Path):
    day = date(2025, 1, 1)
    mark = _write(tmp_path / "mark.zip", _price_zip("mark.csv", offset=0.1))
    index = _write(tmp_path / "index.zip", _price_zip("index.csv"))
    premium = _write(tmp_path / "premium.zip", _price_zip("premium.csv", offset=-100.0))
    metrics = _write(tmp_path / "metrics.zip", _metrics_zip("metrics.csv"))
    funding = _write(tmp_path / "funding.zip", _funding_zip("funding.csv"))
    depth = _write(tmp_path / "depth.zip", _book_depth_zip("depth.csv"))

    basis_frame, basis_audit = build_basis(
        [mark], [index], [premium], symbol="BTCUSDT", start=day, through=day
    )
    oi_frame, oi_audit = build_open_interest(
        [metrics], symbol="BTCUSDT", start=day, through=day
    )
    funding_frame, funding_audit = build_funding(
        [funding], symbol="BTCUSDT", start=day, through=day
    )
    depth_frame, depth_audit = build_book_depth(
        [depth], symbol="BTCUSDT", start=day, through=day
    )

    assert len(basis_frame) == len(oi_frame) == len(depth_frame) == 288
    assert oi_frame["event_time"].dtype.kind == "i"
    assert (basis_frame["available_at"] > basis_frame["event_time"]).all()
    assert (oi_frame["available_at"] - oi_frame["event_time"] == 300_000).all()
    assert (depth_frame["available_at"] - depth_frame["event_time"] == 300_000).all()
    assert funding_frame.loc[0, "available_at"] == funding_frame.loc[0, "event_time"]
    assert funding_frame.loc[0, "event_time"] % 1000 == 15
    assert basis_audit["missing_intervals"] == 0
    assert oi_audit["availability"] == "source_time_plus_5m"
    assert funding_audit["max_gap_hours"] == pytest.approx(8.0)
    assert depth_audit["minimum_snapshots_per_bar"] == 2
    assert depth_audit["source_semantics"].endswith("not_l2_or_spread")


def test_funding_rejects_incomplete_requested_day(tmp_path: Path):
    funding = _write(
        tmp_path / "funding.zip",
        _zip_csv(
            "funding.csv",
            ["calc_time", "funding_interval_hours", "last_funding_rate"],
            [["2025-01-01 00:00:00", 8, 0.0001], ["2025-01-01 08:00:00", 8, 0.0002]],
        ),
    )

    with pytest.raises(DerivativesDataIntegrityError, match="continuously cover"):
        build_funding(
            [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
        )


@pytest.mark.parametrize("gap_ms, accepted", [
    (28_860_000, True),
    (28_860_001, False),
])
def test_funding_internal_gap_has_exact_millisecond_boundary(
    tmp_path: Path, gap_ms: int, accepted: bool
):
    start_ms = 1_735_689_600_000
    funding = _write(
        tmp_path / "funding.zip",
        _funding_zip_at(
            "funding.csv",
            [start_ms, start_ms + gap_ms, start_ms + gap_ms + 28_800_000],
        ),
    )

    if accepted:
        _, audit = build_funding(
            [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
        )
        assert audit["max_gap_hours"] == pytest.approx(28_860_000 / 3_600_000)
    else:
        with pytest.raises(DerivativesDataIntegrityError, match="continuously cover"):
            build_funding(
                [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
            )


@pytest.mark.parametrize("first_offset_ms, accepted", [
    (28_860_000, True),
    (28_860_001, False),
])
def test_funding_request_start_has_exact_millisecond_boundary(
    tmp_path: Path, first_offset_ms: int, accepted: bool
):
    start_ms = 1_735_689_600_000
    funding = _write(
        tmp_path / "funding.zip",
        _funding_zip_at(
            "funding.csv",
            [start_ms + first_offset_ms, start_ms + first_offset_ms + 28_800_000],
        ),
    )

    if accepted:
        build_funding(
            [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
        )
    else:
        with pytest.raises(DerivativesDataIntegrityError, match="continuously cover"):
            build_funding(
                [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
            )


@pytest.mark.parametrize("end_gap_ms, accepted", [
    (28_860_000, True),
    (28_860_001, False),
])
def test_funding_request_end_has_exact_millisecond_boundary(
    tmp_path: Path, end_gap_ms: int, accepted: bool
):
    start_ms = 1_735_689_600_000
    end_ms = start_ms + 24 * 3_600_000
    funding = _write(
        tmp_path / "funding.zip",
        _funding_zip_at(
            "funding.csv",
            [start_ms, start_ms + 8 * 3_600_000, end_ms - end_gap_ms],
        ),
    )

    if accepted:
        build_funding(
            [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
        )
    else:
        with pytest.raises(DerivativesDataIntegrityError, match="continuously cover"):
            build_funding(
                [funding], symbol="BTCUSDT", start=date(2025, 1, 1), through=date(2025, 1, 1)
            )


def test_versioned_release_is_atomic_and_immutable(tmp_path: Path, monkeypatch):
    for relative in REQUIRED_DIRECTORIES:
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    fixture_root = tmp_path / "raw" / "derivatives_archive" / "fixtures"
    fixtures = {
        "metrics": _write(fixture_root / "metrics.zip", _metrics_zip("metrics.csv")),
        "mark_price": _write(fixture_root / "mark.zip", _price_zip("mark.csv", offset=0.1)),
        "index_price": _write(fixture_root / "index.zip", _price_zip("index.csv")),
        "premium_index": _write(fixture_root / "premium.zip", _price_zip("premium.csv", offset=-100.0)),
        "funding": _write(fixture_root / "funding.zip", _funding_zip("funding.csv")),
        "book_depth": _write(fixture_root / "depth.zip", _book_depth_zip("depth.csv")),
    }

    def fake_download(specs, archive_root, workers):
        return [
            DownloadResult(
                spec=spec,
                status="cached",
                path=str(fixtures[spec.source]),
                size=fixtures[spec.source].stat().st_size,
                sha256=hashlib.sha256(fixtures[spec.source].read_bytes()).hexdigest(),
                elapsed_seconds=0.0,
            )
            for spec in specs
        ]

    monkeypatch.setattr(sync_derivatives, "download_archives", fake_download)
    kwargs = dict(
        root=tmp_path,
        release_id="v5_test_release",
        symbols=("BTCUSDT",),
        datasets=sync_derivatives.LOGICAL_DATASETS,
        start=date(2025, 1, 1),
        through=date(2025, 1, 1),
        workers=1,
    )
    interrupted = (
        tmp_path / "normalized" / "derivatives" / ".v5_test_release.staging"
    )
    interrupted.mkdir()
    (interrupted / "partial.parquet").write_bytes(b"partial")
    manifest = sync_derivatives.run_sync(**kwargs)

    release = tmp_path / "normalized" / "derivatives" / "releases" / "v5_test_release"
    assert manifest["status"] == "completed"
    assert manifest["output_count"] == 4
    assert manifest["resumed_from_interrupted_staging"] is True
    assert manifest["manifest_sha256"] == canonical_manifest_sha256(manifest)
    assert (release / "manifest.json").is_file()
    assert not (release.parent / ".v5_test_release.staging").exists()
    with pytest.raises(FileExistsError, match="already exists"):
        sync_derivatives.run_sync(**kwargs)
