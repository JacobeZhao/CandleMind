from __future__ import annotations

import hashlib
import io
import os
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.app.services.market_data_sync import (
    ArchiveSpec,
    DataIntegrityError,
    archive_specs,
    audit_frame,
    build_canonical_5m,
    download_archive,
    missing_daily_archive_specs,
    parse_checksum,
    publish_staging_directory,
    publish_staged_files,
    read_archive,
    resample_ohlcv,
)


def _rows(count: int = 12, *, start_ms: int = 1_700_000_000_000) -> list[list]:
    rows = []
    for index in range(count):
        opened = start_ms + index * 300_000
        price = 100.0 + index
        rows.append([
            opened, price, price + 2, price - 1, price + 1, 10 + index,
            opened + 299_999, 1_000 + index, 10, 6 + index, 600, 0,
        ])
    return rows


def _zip_payload(rows: list[list], *, header: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        lines = []
        if header:
            lines.append(",".join([
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore",
            ]))
        lines.extend(",".join(map(str, row)) for row in rows)
        archive.writestr("bars.csv", "\n".join(lines))
    return output.getvalue()


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.payload


class _Opener:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        return _Response(self.payloads[request.full_url])


def test_archive_specs_use_complete_months_and_open_month_days():
    specs = archive_specs(
        "BTCUSDT",
        start_month=date(2026, 5, 1),
        through=date(2026, 7, 2),
    )
    assert [(item.period, item.key) for item in specs] == [
        ("monthly", "2026-05"),
        ("monthly", "2026-06"),
        ("daily", "2026-07-01"),
        ("daily", "2026-07-02"),
    ]


def test_download_archive_verifies_checksum_and_reuses_cache(tmp_path: Path):
    spec = ArchiveSpec("BTCUSDT", "monthly", "2026-06")
    payload = _zip_payload(_rows(2))
    digest = hashlib.sha256(payload).hexdigest()
    opener = _Opener({
        f"{spec.url}.CHECKSUM": f"{digest}  {spec.filename}\n".encode(),
        spec.url: payload,
    })

    first = download_archive(spec, tmp_path, opener_factory=lambda: opener)
    second = download_archive(spec, tmp_path, opener_factory=lambda: opener)

    assert first.status == "downloaded"
    assert second.status == "cached"
    assert len([url for url in opener.calls if url == spec.url]) == 1
    assert len([url for url in opener.calls if url.endswith(".CHECKSUM")]) == 1


def test_parse_checksum_rejects_malformed_payload():
    with pytest.raises(DataIntegrityError, match="invalid Binance checksum"):
        parse_checksum(b"not-a-checksum")


def test_read_archive_normalizes_microseconds_and_header(tmp_path: Path):
    rows = _rows(2)
    rows[0][0] *= 1000
    rows[0][6] *= 1000
    path = tmp_path / "bars.zip"
    path.write_bytes(_zip_payload(rows))

    frame = read_archive(path)

    assert len(frame) == 2
    assert frame.iloc[0]["open_time"] == _rows(1)[0][0]
    assert frame["open"].dtype.kind == "f"


def test_build_canonical_deduplicates_and_resamples_complete_buckets(tmp_path: Path):
    rows = _rows(12, start_ms=1_699_999_200_000)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first.write_bytes(_zip_payload(rows[:7]))
    second.write_bytes(_zip_payload(rows[6:]))

    frame = build_canonical_5m([first, second])
    fifteen = resample_ohlcv(frame, "15m")

    assert len(frame) == 12
    assert len(fifteen) == 4
    assert fifteen.iloc[0]["open"] == rows[0][1]
    assert fifteen.iloc[0]["close"] == rows[2][4]
    assert fifteen.iloc[0]["volume"] == sum(row[5] for row in rows[:3])
    audit = audit_frame(fifteen, "BTCUSDT", "15m")
    assert audit.gap_events == 0


def test_audit_rejects_gap_and_invalid_ohlc(tmp_path: Path):
    path = tmp_path / "bars.zip"
    rows = _rows(4)
    path.write_bytes(_zip_payload(rows))
    frame = build_canonical_5m([path])
    with_gap = frame.drop(index=1).reset_index(drop=True)
    with pytest.raises(DataIntegrityError, match="gaps"):
        audit_frame(with_gap, "BTCUSDT", "5m")
    repair = missing_daily_archive_specs(with_gap, "BTCUSDT")
    assert repair == [
        ArchiveSpec(
            "BTCUSDT",
            "daily",
            pd.Timestamp(int(frame.iloc[1]["open_time"]), unit="ms", tz="UTC")
            .date()
            .isoformat(),
        )
    ]

    invalid = frame.copy()
    invalid.loc[1, "high"] = invalid.loc[1, "low"] - 1
    with pytest.raises(DataIntegrityError, match="invalid OHLCV"):
        audit_frame(invalid, "BTCUSDT", "5m")


def test_publish_staging_replaces_complete_directory(tmp_path: Path):
    destination = tmp_path / "ohlcv"
    staging = tmp_path / "staging"
    destination.mkdir()
    staging.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    (staging / "new.txt").write_text("new", encoding="utf-8")

    publish_staging_directory(staging, destination)

    assert not staging.exists()
    assert not (destination / "old.txt").exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_files_preserves_unrelated_datasets(tmp_path: Path):
    destination = tmp_path / "ohlcv"
    staging = tmp_path / "staging"
    destination.mkdir()
    staging.mkdir()
    (destination / "ETHUSDT_5m.parquet").write_bytes(b"eth")
    (destination / "BTCUSDT_5m.parquet").write_bytes(b"old-btc")
    (staging / "BTCUSDT_5m.parquet").write_bytes(b"new-btc")

    publish_staged_files(staging, destination)

    assert (destination / "ETHUSDT_5m.parquet").read_bytes() == b"eth"
    assert (destination / "BTCUSDT_5m.parquet").read_bytes() == b"new-btc"
    assert not staging.exists()


def test_publish_staged_files_rolls_back_on_failure(tmp_path: Path, monkeypatch):
    destination = tmp_path / "ohlcv"
    staging = tmp_path / "staging"
    destination.mkdir()
    staging.mkdir()
    (destination / "BTCUSDT_15m.parquet").write_bytes(b"old-15m")
    (destination / "BTCUSDT_5m.parquet").write_bytes(b"old-5m")
    (staging / "BTCUSDT_15m.parquet").write_bytes(b"new-15m")
    (staging / "BTCUSDT_5m.parquet").write_bytes(b"new-5m")
    real_replace = os.replace

    def fail_second_publish(source, target):
        if Path(source).parent == staging and Path(source).name == "BTCUSDT_5m.parquet":
            raise OSError("publish failed")
        real_replace(source, target)

    monkeypatch.setattr(
        "backend.app.services.market_data_sync.os.replace", fail_second_publish
    )
    with pytest.raises(OSError, match="publish failed"):
        publish_staged_files(staging, destination)

    assert (destination / "BTCUSDT_15m.parquet").read_bytes() == b"old-15m"
    assert (destination / "BTCUSDT_5m.parquet").read_bytes() == b"old-5m"
