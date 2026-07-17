import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from backend.app.services import datafeed
from backend.app.services.feature_builder import (
    align_completed_features,
    volume_features,
)


def _bars(start: str, periods: int, freq: str = "5min") -> pd.DataFrame:
    open_time = pd.date_range(start, periods=periods, freq=freq)
    price = np.linspace(100.0, 101.0, periods)
    return pd.DataFrame(
        {
            "open_time": open_time,
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + 0.25,
            "volume": np.full(periods, 10.0),
            "taker_buy_base": np.full(periods, 5.0),
        }
    )


def _funding_frame(start: str, end: str, rate: float = 0.0001) -> pd.DataFrame:
    event_time = pd.date_range(start, end, freq="8h", tz="UTC")
    return pd.DataFrame(
        {
            "funding_time": event_time.astype("int64") // 1_000_000,
            "rate": np.full(len(event_time), rate),
        }
    )


def _funding_zip(rows) -> bytes:
    csv_rows = ["calc_time,funding_interval_hours,last_funding_rate"]
    csv_rows.extend(f"{timestamp},8,{rate}" for timestamp, rate in rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("funding.csv", "\n".join(csv_rows))
    return output.getvalue()


class _FakeOpener:
    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or {}
        self.error = error
        self.calls = []

    def open(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        for month, payload in self.payloads.items():
            if url.endswith(f"-{month}.zip"):
                return io.BytesIO(payload)
        raise AssertionError(f"unexpected funding download: {url}")


def test_funding_alignment_preserves_event_history_and_variation():
    event_time = pd.date_range("2025-01-01", periods=100, freq="8h")
    rates = np.linspace(-0.0002, 0.0003, len(event_time))
    funding = pd.DataFrame(
        {
            "funding_time": event_time.astype("int64") // 1_000_000,
            "rate": rates,
        }
    )
    bars = _bars("2025-01-25", periods=8 * 24 * 12)

    result = volume_features(bars, "5m", funding_df=funding)

    assert result["5m_funding_rate"].nunique() > 1
    assert result["5m_funding_z3d"].notna().all()
    first_time = result.index[0]
    expected = funding.loc[event_time <= first_time, "rate"].iloc[-1]
    assert result.loc[first_time, "5m_funding_rate"] == expected
    assert result.loc[first_time, "5m_funding_rate"] != funding["rate"].iloc[-1]


def test_completed_source_bar_is_not_visible_before_next_open():
    source = pd.DataFrame(
        {
            "open_time": pd.date_range("2025-01-01", periods=3, freq="1h"),
            "signal": [10.0, 20.0, 30.0],
        }
    )
    base = pd.to_datetime(
        ["2025-01-01 00:55", "2025-01-01 01:00", "2025-01-01 01:55", "2025-01-01 02:00"]
    )

    result = align_completed_features(source, base)

    assert np.isnan(result.loc[0, "signal"])
    assert result.loc[1, "signal"] == 10.0
    assert result.loc[2, "signal"] == 10.0
    assert result.loc[3, "signal"] == 20.0


def test_normalized_refresh_prefers_complete_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(datafeed, "PARQUET_DIR", tmp_path)
    stale = _bars("2025-01-01", periods=1)
    stale["taker_buy_base"] = np.nan
    fresh = stale.copy()
    fresh["taker_buy_base"] = 4.25
    merged = pd.concat([stale, fresh], ignore_index=True)

    datafeed._write_pq(merged, "BTCUSDT", "1h")
    saved = pd.read_parquet(tmp_path / "BTCUSDT_1h.parquet")

    assert len(saved) == 1
    assert saved.loc[0, "taker_buy_base"] == 4.25


def test_coverage_rejects_missing_taker_field():
    start_ms = datafeed._to_ms("2025-01-01")
    end_ms = datafeed._to_ms("2025-01-02")
    frame = pd.DataFrame(
        {
            "open_time": [start_ms, end_ms],
            "taker_buy_base": [np.nan, np.nan],
        }
    )

    assert not datafeed._covered(frame, start_ms, end_ms, "1h")
    frame["taker_buy_base"] = [1.0, 2.0]
    assert datafeed._covered(frame, start_ms, end_ms, "1h")


def test_funding_cache_downloads_only_missing_month_and_merges_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(datafeed, "FUNDING_DIR", tmp_path)
    cache_path = tmp_path / "BTCUSDT.parquet"
    january = _funding_frame("2025-01-02", "2025-01-31 16:00", rate=0.0001)
    stale_february = _funding_frame("2025-02-01", "2025-02-01", rate=0.0002)
    pd.concat([january, stale_february], ignore_index=True).to_parquet(cache_path, index=False)

    duplicate_time = datafeed._to_ms("2025-02-01")
    downloaded = _funding_zip(
        [
            (duplicate_time, 0.0009),
            (duplicate_time + 8 * 60 * 60 * 1000, 0.0010),
            (duplicate_time + 16 * 60 * 60 * 1000, 0.0011),
        ]
    )
    opener = _FakeOpener({"2025-02": downloaded})
    monkeypatch.setattr(datafeed, "_opener", lambda proxy: opener)

    result = datafeed.load_funding("BTCUSDT", "2025-02-01", "2025-02-02")

    assert len(opener.calls) == 1
    assert opener.calls[0][0].endswith("BTCUSDT-fundingRate-2025-02.zip")
    assert result["funding_time"].is_monotonic_increasing
    assert not result["funding_time"].duplicated().any()
    assert result.loc[result["funding_time"] == duplicate_time, "rate"].item() == 0.0009
    saved = pd.read_parquet(cache_path)
    assert saved["funding_time"].min() == january["funding_time"].min()

    no_network = _FakeOpener(error=AssertionError("complete cache must not use network"))
    monkeypatch.setattr(datafeed, "_opener", lambda proxy: no_network)
    cached_result = datafeed.load_funding("BTCUSDT", "2025-02-01", "2025-02-02")

    pd.testing.assert_frame_equal(cached_result, result)
    assert no_network.calls == []


def test_funding_cache_reports_missing_month_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(datafeed, "FUNDING_DIR", tmp_path)
    cache_path = tmp_path / "BTCUSDT.parquet"
    cached = _funding_frame("2025-01-02", "2025-01-31 16:00")
    cached.to_parquet(cache_path, index=False)
    opener = _FakeOpener(error=OSError("network unavailable"))
    monkeypatch.setattr(datafeed, "_opener", lambda proxy: opener)

    with pytest.raises(datafeed.FundingDataIncompleteError, match="2025-02"):
        datafeed.load_funding("BTCUSDT", "2025-02-01", "2025-02-02")

    pd.testing.assert_frame_equal(pd.read_parquet(cache_path), cached)
