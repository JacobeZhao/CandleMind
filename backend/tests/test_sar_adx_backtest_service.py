from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.app.services import sar_adx_backtest as service

from backend.app.services.sar_adx_backtest import (
    MAX_CURVE_POINTS,
    MAX_DETAIL_ROWS,
    SarAdxWindowError,
    _verified_symbols,
    _validate_coverage,
    build_response,
    frozen_config,
)
from backend.app.strategies.sar_pyramid_backtrader import BacktraderSarResult


def _result(*, curve_rows: int = 3, detail_rows: int = 2) -> BacktraderSarResult:
    times = pd.date_range("2025-01-01", periods=curve_rows, freq="5min", tz="UTC")
    detail_times = pd.date_range("2025-01-01", periods=detail_rows, freq="1h", tz="UTC")
    fills = pd.DataFrame(
        {
            "time": detail_times,
            "action": ["open"] * detail_rows,
            "direction": [1] * detail_rows,
            "layer": [1] * detail_rows,
            "size": [1.0] * detail_rows,
            "price": [100.0] * detail_rows,
            "value": [100.0] * detail_rows,
            "commission": [0.1] * detail_rows,
            "order_ref": list(range(detail_rows)),
        }
    )
    funding = pd.DataFrame(
        {
            "time": detail_times,
            "rate": [0.001] * detail_rows,
            "notional": [100.0] * detail_rows,
            "payment": [-0.1] * detail_rows,
        }
    )
    trades = pd.DataFrame(
        {
            "entry_time": detail_times,
            "exit_time": detail_times + pd.Timedelta(minutes=30),
            "bar_length": [6] * detail_rows,
            "direction": [1] * detail_rows,
            "max_layers": [2] * detail_rows,
            "exit_reason": ["reverse_close"] * detail_rows,
            "gross_pnl": [2.0 if index % 2 == 0 else -1.0 for index in range(detail_rows)],
            "net_pnl_before_funding": [
                1.8 if index % 2 == 0 else -1.2 for index in range(detail_rows)
            ],
        }
    )
    equity = pd.DataFrame(
        {
            "time": times,
            "equity": [10_000.0 + index for index in range(curve_rows)],
            "position_size": [0.0] * curve_rows,
        }
    )
    return BacktraderSarResult(
        metrics={
            "engine": "backtrader",
            "engine_version": "1.9",
            "initial_cash": 10_000.0,
            "final_equity": 10_001.0,
            "total_return": 0.0001,
            "max_drawdown": 0.0,
            "fill_count": detail_rows,
            "trade_count": detail_rows,
            "win_rate": 0.5,
            "profit_factor_before_funding": 1.5,
            "commission": detail_rows * 0.1,
            "turnover": detail_rows * 100.0,
            "funding_pnl": detail_rows * -0.1,
            "rejected_add_count": 0,
        },
        fills=fills,
        funding=funding,
        trades=trades,
        equity=equity,
    )


def test_build_response_exposes_explicit_before_funding_metrics() -> None:
    config = frozen_config(initial_capital=10_000, fee_rate=0.001, slippage_bps=2)
    payload = build_response(
        _result(),
        config=config,
        start=pd.Timestamp("2025-01-01", tz="UTC"),
        end=pd.Timestamp("2025-02-01", tz="UTC"),
        data_manifest={"release_id": "data-v1", "manifest_sha256": "a" * 64},
        funding_manifest={"manifest_sha256": "b" * 64},
        bar_count=100,
    )

    assert payload["strategy"]["status"] == "diagnostic"
    assert payload["strategy"]["symbol"] == "SOLUSDT"
    assert payload["strategy"]["parameter_origin"] == "SOLUSDT"
    assert payload["window"]["semantics"] == "[start,end)"
    assert payload["metrics"]["final_equity"] == 10_001.0
    assert payload["metrics"]["funding_pnl"] == pytest.approx(-0.2)
    assert payload["metrics"]["win_rate_before_funding"] == 0.5
    assert payload["metrics"]["payoff_ratio_before_funding"] == 1.5
    assert "slippage_cost" not in payload["metrics"]
    assert payload["trades"][0]["direction"] == 1
    json.dumps(payload, allow_nan=False)


def test_build_response_bounds_curves_and_details() -> None:
    result = _result(
        curve_rows=MAX_CURVE_POINTS + 17,
        detail_rows=MAX_DETAIL_ROWS + 3,
    )
    payload = build_response(
        result,
        config=frozen_config(initial_capital=10_000, fee_rate=0.001, slippage_bps=2),
        start=pd.Timestamp("2025-01-01", tz="UTC"),
        end=pd.Timestamp("2025-02-01", tz="UTC"),
        data_manifest={"manifest_sha256": "a" * 64},
        funding_manifest={"manifest_sha256": "b" * 64},
        bar_count=1,
    )

    assert len(payload["equity_curve"]) == MAX_CURVE_POINTS
    assert len(payload["drawdown_curve"]) == MAX_CURVE_POINTS
    assert len(payload["trades"]) == MAX_DETAIL_ROWS
    assert len(payload["fills"]) == MAX_DETAIL_ROWS
    assert len(payload["funding"]) == MAX_DETAIL_ROWS
    assert all(payload["execution"]["truncated"].values())


def test_coverage_requires_both_ohlcv_and_observed_funding() -> None:
    ohlcv = {
        "window": {
            "start": "2024-01-01T00:00:00Z",
            "end": "2026-07-01T00:00:00Z",
        }
    }
    funding = {
        "start_utc": "2024-02-01T00:00:00Z",
        "end_utc": "2026-06-30T16:00:00Z",
    }

    with pytest.raises(SarAdxWindowError, match="outside verified"):
        _validate_coverage(
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2024-03-01", tz="UTC"),
            ohlcv,
            funding,
        )


def test_verified_symbols_is_sorted_manifest_intersection() -> None:
    data_manifest = {
        "outputs": [
            {"kind": "ohlcv", "symbol": "SOLUSDT"},
            {"kind": "ohlcv", "symbol": "BTCUSDT"},
            {"kind": "point_in_time_universe"},
        ]
    }
    funding_manifest = {
        "outputs": [
            {"dataset": "funding", "symbol": "ETHUSDT"},
            {"dataset": "funding", "symbol": "SOLUSDT"},
        ]
    }

    assert _verified_symbols(data_manifest, funding_manifest) == ("SOLUSDT",)


def test_run_isolates_ohlcv_funding_and_pit_by_symbol(monkeypatch) -> None:
    data_manifest = {
        "release_id": "ohlcv-v1",
        "manifest_sha256": "a" * 64,
        "outputs": [
            {
                "kind": "ohlcv",
                "symbol": symbol,
                "path": f"ohlcv/{symbol}.parquet",
                "window": {
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2026-07-01T00:00:00Z",
                },
            }
            for symbol in ("BTCUSDT", "SOLUSDT")
        ]
        + [{"kind": "point_in_time_universe", "path": "universe.parquet"}],
    }
    funding_manifest = {
        "release_id": "funding-v1",
        "manifest_sha256": "b" * 64,
        "outputs": [
            {
                "dataset": "funding",
                "symbol": symbol,
                "path": f"funding/{symbol}.parquet",
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2026-06-30T16:00:00Z",
            }
            for symbol in ("BTCUSDT", "SOLUSDT")
        ],
    }
    bars = pd.DataFrame(
        {
            "open_time": [1, 2],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
        }
    )
    universe = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "SOLUSDT"],
            "eligible": [True, True],
        }
    )
    funding = pd.DataFrame({"symbol": ["BTCUSDT"]})
    captured = {}

    monkeypatch.setattr(service, "verify_market_release", lambda _path: data_manifest)
    monkeypatch.setattr(
        service, "verify_observed_funding_release", lambda _path: funding_manifest
    )

    def fake_read(path, **_kwargs):
        return universe if str(path).endswith("universe.parquet") else bars

    monkeypatch.setattr(service.pd, "read_parquet", fake_read)

    def fake_funding(_path, symbol, *, manifest):
        captured["funding_symbol"] = symbol
        captured["funding_manifest"] = manifest
        return funding, funding_manifest["outputs"][0]

    monkeypatch.setattr(service, "load_observed_funding_symbol", fake_funding)

    def fake_prepare(input_bars, *, funding, eligibility, **_kwargs):
        captured["bars"] = input_bars
        captured["funding"] = funding
        captured["eligibility_symbols"] = eligibility["symbol"].tolist()
        return pd.DataFrame({"time": [1, 2]})

    monkeypatch.setattr(service, "prepare_backtrader_signal_frame", fake_prepare)
    monkeypatch.setattr(service, "run_backtrader_sar_pyramid", lambda *_a, **_k: _result())

    payload = service.run_sar_adx_backtest(
        symbol="BTCUSDT",
        start=pd.Timestamp("2025-01-01", tz="UTC"),
        end=pd.Timestamp("2025-02-01", tz="UTC"),
        initial_capital=10_000,
        fee_rate=0.001,
        slippage_bps=2,
    )

    assert captured["funding_symbol"] == "BTCUSDT"
    assert captured["eligibility_symbols"] == ["BTCUSDT"]
    assert captured["bars"] is bars
    assert captured["funding"] is funding
    assert payload["strategy"]["symbol"] == "BTCUSDT"
    assert payload["data_lineage"]["ohlcv_release_id"] == "ohlcv-v1"
    assert "path" not in json.dumps(payload).lower()
