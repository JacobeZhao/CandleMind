"""API adapter for the multi-symbol SAR/ADX Backtrader diagnostic."""

from __future__ import annotations

from dataclasses import asdict, replace
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..datastore import MARKET_ROOT
from ..strategies.sar_pyramid import SarPyramidConfig
from ..strategies.sar_adx_config import sar_adx_v3_config
from ..strategies.sar_pyramid_backtrader import (
    BacktraderSarResult,
    prepare_backtrader_signal_frame,
    run_backtrader_sar_pyramid,
)
from .funding_release import (
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from .market_release import verify_market_release


SYMBOL = "SOLUSDT"
DATA_RELEASE_ID = "ema_pit_30_20240101_20260630_v2"
FUNDING_RELEASE_ID = "funding_observed_30_20240101_20260630_v1"
DATA_RELEASE = MARKET_ROOT / "normalized" / "ema" / "releases" / DATA_RELEASE_ID
FUNDING_RELEASE = (
    MARKET_ROOT / "normalized" / "derivatives" / "releases" / FUNDING_RELEASE_ID
)
MAX_CURVE_POINTS = 2_000
MAX_DETAIL_ROWS = 1_000


class SarAdxDataUnavailableError(RuntimeError):
    """The configured immutable market-data release cannot be used."""


class SarAdxWindowError(ValueError):
    """The requested window is outside verified source coverage."""


def get_sar_adx_capabilities() -> dict[str, Any]:
    """Return the path-free intersection of the verified immutable releases."""

    try:
        data_manifest = verify_market_release(DATA_RELEASE)
        funding_manifest = verify_observed_funding_release(FUNDING_RELEASE)
        universe_record = _unique_output(data_manifest, kind="point_in_time_universe")
        universe = pd.read_parquet(DATA_RELEASE / universe_record["path"])
        symbols = _verified_symbols(data_manifest, funding_manifest)
        coverage = []
        for symbol in symbols:
            ohlcv_record = _unique_output(data_manifest, kind="ohlcv", symbol=symbol)
            funding_record = _unique_output(
                funding_manifest, dataset="funding", symbol=symbol
            )
            available_start, available_end = _available_window(
                ohlcv_record, funding_record
            )
            eligible = universe.loc[
                universe["symbol"].eq(symbol) & universe["eligible"].astype(bool)
            ]
            coverage.append(
                {
                    "symbol": symbol,
                    "start": available_start.isoformat(),
                    "end": available_end.isoformat(),
                    "semantics": "[start,end)",
                    "eligible_window_count": int(len(eligible)),
                }
            )
    except Exception as exc:
        raise SarAdxDataUnavailableError(
            "The verified SAR/ADX market-data release is unavailable."
        ) from exc

    return {
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "coverage": coverage,
        "data_lineage": _data_lineage(data_manifest, funding_manifest),
        "strategy_scope": {
            "status": "diagnostic",
            "parameter_origin": "SOLUSDT",
            "cross_symbol_use": "SOL-tuned parameters; results are diagnostic only",
        },
    }


def frozen_config(*, initial_capital: float, fee_rate: float, slippage_bps: float) -> SarPyramidConfig:
    config = replace(
        sar_adx_v3_config(initial_cash=initial_capital),
        fee_rate=fee_rate,
        slippage_rate=slippage_bps / 10_000.0,
    )
    config.validate()
    return config


def run_sar_adx_backtest(
    *,
    symbol: str = SYMBOL,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital: float,
    fee_rate: float,
    slippage_bps: float,
) -> dict[str, Any]:
    """Verify frozen releases, execute Backtrader, and return bounded JSON data."""

    start_at, end_at = _utc(start), _utc(end)
    config = frozen_config(
        initial_capital=initial_capital,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    try:
        data_manifest = verify_market_release(DATA_RELEASE)
        funding_manifest = verify_observed_funding_release(FUNDING_RELEASE)
        symbols = _verified_symbols(data_manifest, funding_manifest)
        if symbol not in symbols:
            raise SarAdxWindowError(
                "Requested symbol is not in the verified OHLCV/funding intersection."
            )
        ohlcv_record = _unique_output(data_manifest, kind="ohlcv", symbol=symbol)
        funding_record = _unique_output(
            funding_manifest, dataset="funding", symbol=symbol
        )
        universe_record = _unique_output(data_manifest, kind="point_in_time_universe")
        _validate_coverage(start_at, end_at, ohlcv_record, funding_record)

        bars = pd.read_parquet(
            DATA_RELEASE / ohlcv_record["path"],
            columns=["open_time", "open", "high", "low", "close"],
        )
        universe = pd.read_parquet(DATA_RELEASE / universe_record["path"])
        funding, _ = load_observed_funding_symbol(
            FUNDING_RELEASE, symbol, manifest=funding_manifest
        )
    except SarAdxWindowError:
        raise
    except Exception as exc:
        raise SarAdxDataUnavailableError(
            "The verified SAR/ADX market-data release is unavailable."
        ) from exc

    eligibility = universe.loc[universe["symbol"] == symbol]
    try:
        tape = prepare_backtrader_signal_frame(
            bars,
            funding=funding,
            eligibility=eligibility,
            start=start_at,
            end=end_at,
            config=config,
        )
        result = run_backtrader_sar_pyramid(tape, config=config)
    except Exception as exc:
        raise SarAdxDataUnavailableError(
            "The verified SAR/ADX data could not produce an executable tape."
        ) from exc

    return build_response(
        result,
        symbol=symbol,
        config=config,
        start=start_at,
        end=end_at,
        data_manifest=data_manifest,
        funding_manifest=funding_manifest,
        bar_count=max(0, len(tape) - 1),
    )


def build_response(
    result: BacktraderSarResult,
    *,
    symbol: str = SYMBOL,
    config: SarPyramidConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    data_manifest: Mapping[str, Any],
    funding_manifest: Mapping[str, Any],
    bar_count: int,
) -> dict[str, Any]:
    equity = result.equity.copy()
    if len(equity):
        equity["drawdown"] = equity["equity"] / equity["equity"].cummax() - 1.0
    trades = result.trades.copy()
    trade_pnl = (
        pd.to_numeric(trades["net_pnl_before_funding"])
        if len(trades) and "net_pnl_before_funding" in trades
        else pd.Series(dtype=float)
    )
    wins = trade_pnl[trade_pnl > 0.0]
    losses = trade_pnl[trade_pnl < 0.0]
    average_win = float(wins.mean()) if len(wins) else None
    average_loss = float(losses.mean()) if len(losses) else None
    payoff_ratio = (
        average_win / abs(average_loss)
        if average_win is not None and average_loss not in (None, 0.0)
        else None
    )
    base_metrics = result.metrics
    metrics = {
        "initial_capital": config.initial_cash,
        "final_equity": base_metrics["final_equity"],
        "total_return": base_metrics["total_return"],
        "max_drawdown": base_metrics["max_drawdown"],
        "trade_count": base_metrics["trade_count"],
        "fill_count": base_metrics["fill_count"],
        "win_rate_before_funding": (
            float((trade_pnl > 0.0).mean()) if len(trade_pnl) else 0.0
        ),
        "profit_factor_before_funding": base_metrics["profit_factor_before_funding"],
        "payoff_ratio_before_funding": payoff_ratio,
        "expectancy_before_funding": float(trade_pnl.mean()) if len(trade_pnl) else 0.0,
        "average_win_before_funding": average_win,
        "average_loss_before_funding": average_loss,
        "commission": base_metrics["commission"],
        "funding_pnl": base_metrics["funding_pnl"],
        "turnover": base_metrics["turnover"],
        "rejected_add_count": base_metrics["rejected_add_count"],
    }
    curve = _sample_frame(equity, MAX_CURVE_POINTS)
    equity_curve = _records(curve, ("time", "equity", "position_size"))
    drawdown_curve = _records(curve, ("time", "drawdown"))
    payload = {
        "strategy": {
            "id": "sar_adx_pyramid_v3",
            "name": (
                "SOL 5m SAR + 1h ADX pyramid V3"
                if symbol == SYMBOL
                else f"{symbol} 5m SAR + 1h ADX pyramid V3"
            ),
            "status": "diagnostic",
            "symbol": symbol,
            "parameter_origin": "SOLUSDT",
            "cross_symbol_use": "SOL-tuned parameters; results are diagnostic only",
            "parameters": asdict(config),
        },
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "semantics": "[start,end)",
        },
        "data_lineage": _data_lineage(data_manifest, funding_manifest),
        "execution": {
            "engine": base_metrics["engine"],
            "engine_version": base_metrics["engine_version"],
            "signal_timing": "completed_5m_bar_next_open",
            "funding_semantics": "cashflow_at_event_open_before_same_open_orders",
            "fee_rate": config.fee_rate,
            "slippage_bps": config.slippage_rate * 10_000.0,
            "bar_count": bar_count,
            "curve_points_total": len(equity),
            "detail_limits": {"curve": MAX_CURVE_POINTS, "rows": MAX_DETAIL_ROWS},
            "truncated": {
                "curve": len(equity) > MAX_CURVE_POINTS,
                "trades": len(result.trades) > MAX_DETAIL_ROWS,
                "fills": len(result.fills) > MAX_DETAIL_ROWS,
                "funding": len(result.funding) > MAX_DETAIL_ROWS,
            },
        },
        "metrics": metrics,
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
        "trades": _records(result.trades.head(MAX_DETAIL_ROWS)),
        "fills": _records(result.fills.head(MAX_DETAIL_ROWS)),
        "funding": _records(result.funding.head(MAX_DETAIL_ROWS)),
    }
    return _finite_json(payload)


def _unique_output(manifest: Mapping[str, Any], **criteria: str) -> Mapping[str, Any]:
    matches = [
        item
        for item in manifest.get("outputs", ())
        if all(item.get(key) == value for key, value in criteria.items())
    ]
    if len(matches) != 1:
        raise ValueError("required release output is not uniquely registered")
    return matches[0]


def _validate_coverage(
    start: pd.Timestamp,
    end: pd.Timestamp,
    ohlcv: Mapping[str, Any],
    funding: Mapping[str, Any],
) -> None:
    available_start, available_end = _available_window(ohlcv, funding)
    if start < available_start or end > available_end:
        raise SarAdxWindowError(
            "Requested dates are outside verified OHLCV/funding coverage."
        )


def _available_window(
    ohlcv: Mapping[str, Any], funding: Mapping[str, Any]
) -> tuple[pd.Timestamp, pd.Timestamp]:
    ohlcv_start = _utc(ohlcv["window"]["start"])
    ohlcv_end = _utc(ohlcv["window"]["end"])
    funding_start = _utc(funding["start_utc"])
    funding_end = _utc(funding["end_utc"]) + pd.Timedelta(hours=8)
    return max(ohlcv_start, funding_start), min(ohlcv_end, funding_end)


def _verified_symbols(
    data_manifest: Mapping[str, Any], funding_manifest: Mapping[str, Any]
) -> tuple[str, ...]:
    ohlcv_symbols = {
        item["symbol"]
        for item in data_manifest.get("outputs", ())
        if item.get("kind") == "ohlcv" and isinstance(item.get("symbol"), str)
    }
    funding_symbols = {
        item["symbol"]
        for item in funding_manifest.get("outputs", ())
        if item.get("dataset") == "funding" and isinstance(item.get("symbol"), str)
    }
    symbols = tuple(sorted(ohlcv_symbols & funding_symbols))
    if not symbols:
        raise ValueError("verified releases have no common symbols")
    return symbols


def _data_lineage(
    data_manifest: Mapping[str, Any], funding_manifest: Mapping[str, Any]
) -> dict[str, str]:
    return {
        "ohlcv_release_id": data_manifest.get("release_id", DATA_RELEASE_ID),
        "ohlcv_manifest_sha256": data_manifest["manifest_sha256"],
        "funding_release_id": funding_manifest.get("release_id", FUNDING_RELEASE_ID),
        "funding_manifest_sha256": funding_manifest["manifest_sha256"],
    }


def _sample_frame(frame: pd.DataFrame, limit: int) -> pd.DataFrame:
    if len(frame) <= limit:
        return frame
    indices = np.linspace(0, len(frame) - 1, limit, dtype=int)
    return frame.iloc[np.unique(indices)]


def _records(frame: pd.DataFrame, columns: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    selected = frame.loc[:, [name for name in (columns or tuple(frame.columns)) if name in frame]]
    return selected.to_dict(orient="records")


def _finite_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    if isinstance(value, (pd.Timestamp,)):
        return _utc(value).isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _utc(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


__all__ = [
    "SarAdxDataUnavailableError",
    "SarAdxWindowError",
    "build_response",
    "frozen_config",
    "get_sar_adx_capabilities",
    "run_sar_adx_backtest",
]
