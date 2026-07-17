import asyncio

import pandas as pd
import pytest

from backend.app.routes import research
from backend.app.services import feature_builder, ml_signal, trend_predictor


NOW = pd.Timestamp("2026-07-15T12:07:30Z")


@pytest.fixture(autouse=True)
def _fixed_utc_now(monkeypatch):
    monkeypatch.setattr(ml_signal, "_utc_now", lambda: NOW)
    ml_signal._FEAT_CACHE.clear()
    yield
    ml_signal._FEAT_CACHE.clear()


def _features(open_times) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": open_times,
            "feature_a": range(len(open_times)),
        }
    )


def _stub_models(monkeypatch, predict=None):
    monkeypatch.setattr(trend_predictor, "load_model", lambda *_args: object())
    monkeypatch.setattr(
        trend_predictor,
        "predict_proba",
        predict or (lambda _bundle, _row: 0.9),
    )


@pytest.mark.parametrize(
    "value",
    [
        1_784_116_800,
        1_784_116_800_000,
        1_784_116_800_000_000,
        1_784_116_800_000_000_000,
        pd.Timestamp("2026-07-15T20:00:00+08:00"),
    ],
)
def test_open_time_units_and_timezones_normalize_to_utc(value):
    assert ml_signal._normalize_timestamp(value) == pd.Timestamp("2026-07-15T12:00:00Z")


def test_latest_features_requests_current_day_and_drops_incomplete_bar(monkeypatch):
    captured = {}
    times = pd.date_range("2026-07-15T07:10:00Z", periods=60, freq="5min")
    times = times.append(pd.DatetimeIndex([pd.Timestamp("2026-07-15T12:05:00Z")]))
    times_ms = times.astype("int64") // 1_000_000

    def fake_build_features(symbol, start, end):
        captured.update(symbol=symbol, start=start, end=end)
        return _features(times_ms)

    monkeypatch.setattr(feature_builder, "build_features", fake_build_features)

    result = ml_signal._latest_features("BTCUSDT")

    assert captured["end"] == "2026-07-16"
    assert result is not None
    assert str(result["open_time"].dt.tz) == "UTC"
    assert result.iloc[-1]["open_time"] == pd.Timestamp("2026-07-15T12:00:00Z")
    assert (result["open_time"] <= pd.Timestamp("2026-07-15T12:00:00Z")).all()


def test_future_feature_bar_fails_closed_before_prediction(monkeypatch):
    _stub_models(
        monkeypatch,
        predict=lambda *_args: pytest.fail("future features must not reach prediction"),
    )
    future = _features([pd.Timestamp("2026-07-15T12:05:00Z")])
    monkeypatch.setattr(ml_signal, "_latest_features", lambda *_args: future)

    result = ml_signal.get_ml_signal("BTCUSDT", check_drift_flag=False)

    assert result.action == "observe"
    assert result.pos_size_mult == 0.0
    assert result.feature_timestamp == "2026-07-15T12:05:00Z"
    assert result.data_age_seconds == 150.0
    assert result.feature_fresh is False
    assert "incomplete" in result.note.lower()


def test_stale_cached_feature_bar_fails_closed(monkeypatch):
    _stub_models(
        monkeypatch,
        predict=lambda *_args: pytest.fail("stale cached features must not reach prediction"),
    )
    stale_times = pd.date_range("2026-07-15T06:50:00Z", periods=60, freq="5min")
    stale = _features(stale_times.astype("int64") // 1_000_000_000)
    current_slot = int(NOW.floor("5min").timestamp())
    ml_signal._FEAT_CACHE["BTCUSDT"] = (current_slot, stale)

    result = ml_signal.get_ml_signal("BTCUSDT", check_drift_flag=False)

    assert result.action == "observe"
    assert result.pos_size_mult == 0.0
    assert result.feature_timestamp == "2026-07-15T11:45:00Z"
    assert result.data_age_seconds > ml_signal.MAX_FEATURE_AGE_SECONDS
    assert result.feature_fresh is False
    assert "stale" in result.note.lower()


def test_signal_api_exposes_freshness_fields(monkeypatch):
    signal = ml_signal.SignalResult(
        symbol="BTCUSDT",
        feature_timestamp="2026-07-15T12:00:00Z",
        data_age_seconds=450.0,
        feature_fresh=True,
    )
    monkeypatch.setattr(ml_signal, "get_ml_signal", lambda *_args: signal)

    response = asyncio.run(research.get_ml_signal_route("BTCUSDT"))

    assert response["feature_timestamp"] == "2026-07-15T12:00:00Z"
    assert response["data_age_seconds"] == 450.0
    assert response["feature_fresh"] is True
