"""Diagnose causal BTC market gates for the multi-symbol SAR pyramid strategy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Sequence
import uuid

from numba import njit
import numpy as np
import pandas as pd

from backend.app.services.funding_release import (
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from backend.app.services.market_release import verify_market_release
from backend.app.strategies.sar_pyramid import adx_regime, parabolic_sar


SCHEMA = "candlemind-multi-symbol-sar-market-gate-diagnostic-v1"
SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "DOGEUSDT",
    "XRPUSDT", "ADAUSDT", "OPUSDT", "BNBUSDT", "LINKUSDT",
    "INJUSDT", "ARBUSDT", "DOTUSDT", "FILUSDT", "NEARUSDT",
)
WINDOWS = (
    ("2024", "2024-01-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026H1", "2026-01-01", "2026-07-01"),
)
GATES = {
    0: "none",
    1: "btc_4h_aligned",
    2: "longs_require_btc_4h_bull",
    3: "shorts_only",
    4: "btc_7d_momentum_aligned",
    5: "btc_4h_and_7d_aligned",
}
ENTRY_STYLES = {
    0: "breakout",
    1: "countertrend_reversal",
    2: "not_overextended",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-release", required=True, type=Path)
    parser.add_argument("--funding-release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def completed_btc_regime(bars: pd.DataFrame) -> pd.DataFrame:
    """Map only completed BTC 4h bars and completed seven-day momentum to 5m rows."""

    frame = _bars_with_time(bars)
    indexed = frame.set_index("open_time")
    four_hour = indexed.resample("4h", label="left", closed="left").agg(
        close=("close", "last"), count=("close", "count")
    )
    four_hour = four_hour.loc[four_hour["count"] == 48].copy()
    four_hour["ema_12"] = four_hour["close"].ewm(span=12, adjust=False).mean()
    four_hour["ema_48"] = four_hour["close"].ewm(span=48, adjust=False).mean()
    four_hour["trend"] = np.sign(four_hour["ema_12"] - four_hour["ema_48"])
    four_hour["momentum_7d"] = np.sign(four_hour["close"].pct_change(42))
    available = four_hour.reset_index()
    available["available_at"] = available["open_time"] + pd.Timedelta(hours=4)
    decision = pd.DataFrame({
        "available_at": frame["open_time"] + pd.Timedelta(minutes=5),
        "_order": np.arange(len(frame)),
    })
    mapped = pd.merge_asof(
        decision.sort_values("available_at"),
        available[["available_at", "trend", "momentum_7d"]].sort_values("available_at"),
        on="available_at", direction="backward", allow_exact_matches=True,
    ).sort_values("_order")
    return pd.DataFrame({
        "btc_4h_trend": mapped["trend"].fillna(0).to_numpy(dtype=np.int8),
        "btc_7d_momentum": mapped["momentum_7d"].fillna(0).to_numpy(dtype=np.int8),
    })


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = run_diagnostic(args)
    print(json.dumps(result, indent=2))


def run_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_release.expanduser().resolve()
    funding_root = args.funding_release.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace diagnostic: {output}")
    data_manifest = verify_market_release(data_root)
    funding_manifest = verify_observed_funding_release(funding_root)
    universe = pd.read_parquet(data_root / "universe_snapshots.parquet")
    btc = pd.read_parquet(data_root / "ohlcv" / "BTCUSDT_5m.parquet")
    btc = _bars_with_time(btc)
    regime = completed_btc_regime(btc)
    market = pd.DataFrame({"open_time": btc["open_time"], **regime.to_dict("series")})

    prepared: dict[str, tuple[np.ndarray, ...]] = {}
    for symbol in SYMBOLS:
        bars = pd.read_parquet(data_root / "ohlcv" / f"{symbol}_5m.parquet")
        funding, _ = load_observed_funding_symbol(
            funding_root, symbol, manifest=funding_manifest
        )
        prepared[symbol] = _prepare_symbol(bars, funding, universe, market, symbol)

    rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    configurations = [
        (style_code, lookback, atr_expansion, gate_code, gate_name)
        for style_code in ENTRY_STYLES
        for lookback in ((12, 24) if style_code == 0 else (24,))
        for atr_expansion in (1.1, 1.2)
        for gate_code, gate_name in GATES.items()
    ]
    for style_code, lookback, atr_expansion, gate_code, gate_name in configurations:
        for window_name, start, end in WINDOWS:
            symbol_metrics = []
            for symbol in SYMBOLS:
                metrics = _simulate_window(
                    prepared[symbol], _ms(start), _ms(end), lookback,
                    atr_expansion, gate_code, style_code,
                )
                record = _metric_record(metrics)
                symbol_metrics.append(record)
                detail_rows.append({
                    "lookback": lookback,
                    "atr_expansion": atr_expansion,
                    "market_gate": gate_name,
                    "entry_style": ENTRY_STYLES[style_code],
                    "window": window_name,
                    "symbol": symbol,
                    **record,
                })
            rows.append({
                "lookback": lookback,
                "atr_expansion": atr_expansion,
                "market_gate": gate_name,
                "entry_style": ENTRY_STYLES[style_code],
                "window": window_name,
                **_portfolio_metrics(symbol_metrics),
            })
    grid = pd.DataFrame(rows)
    detail = pd.DataFrame(detail_rows)
    candidates = _candidate_summary(grid, detail)
    decision = _admission(candidates)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        grid.to_parquet(staging / "window_grid.parquet", index=False, compression="zstd")
        detail.to_parquet(staging / "symbol_detail.parquet", index=False, compression="zstd")
        candidates.to_parquet(staging / "candidate_summary.parquet", index=False, compression="zstd")
        report = {
            "schema": SCHEMA,
            "status": "diagnostic_only",
            "historical_periods_previously_inspected": True,
            "strategy_contract": {
                "execution": "completed 5m decision; next 5m open fill",
                "entry": "PSAR reversal plus bounded maturity style and ATR14/ATR14-144bar-mean expansion; three aligned bars; ADX<=35",
                "entry_styles": {
                    "breakout": "directional close beyond prior lookback range",
                    "countertrend_reversal": "directional EMA288 distance<=0 and 72-bar momentum<=0",
                    "not_overextended": "directional EMA288 distance<=1 ATR and 72-bar momentum<=1 ATR",
                },
                "adds": "20% pullback-recapture layers while 1h ADX>=25 and DI agrees",
                "exit": "12 consecutive opposite PSAR bars",
                "costs": {"fee_single_side": 0.001, "slippage_single_side": 0.0002, "funding": "observed"},
                "portfolio": "fixed 15 equal subaccounts with monthly PIT rank<=15 eligibility",
            },
            "market_feature_contract": {
                "btc_4h_trend": "sign(EMA12-EMA48) on last completed 4h bar",
                "btc_7d_momentum": "sign(close/close[-42]-1) on last completed 4h bar",
            },
            "requirements": {"minimum_annual_trades": 600, "all_windows_profitable": True},
            "decision": decision,
            "best_candidates": _json_rows(candidates.head(8)),
        }
        _write_json(staging / "report.json", report)
        names = ("window_grid.parquet", "symbol_detail.parquet", "candidate_summary.parquet", "report.json")
        manifest = {
            "schema": SCHEMA,
            "status": "completed",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_manifest_sha256": data_manifest["manifest_sha256"],
            "funding_manifest_sha256": funding_manifest["manifest_sha256"],
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "artifacts": {name: _evidence(staging / name) for name in names},
        }
        manifest["manifest_sha256"] = _canonical_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        os.rename(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"output": str(output), "decision": decision, "best": _json_rows(candidates.head(3))}


def _prepare_symbol(
    bars: pd.DataFrame,
    funding: pd.DataFrame,
    universe: pd.DataFrame,
    market: pd.DataFrame,
    symbol: str,
) -> tuple[np.ndarray, ...]:
    frame = _bars_with_time(bars)
    frame = frame.loc[
        (frame["open_time"] >= pd.Timestamp("2023-06-01", tz="UTC"))
        & (frame["open_time"] < pd.Timestamp("2026-07-01", tz="UTC"))
    ].reset_index(drop=True)
    psar = parabolic_sar(frame)
    adx = adx_regime(frame, timeframe="1h", period=14, threshold=25.0)
    previous = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr_base = atr.shift(1).rolling(144, min_periods=144).mean()
    ema_288 = frame["close"].ewm(span=288, adjust=False).mean()
    slow_distance = (frame["close"] - ema_288) / atr
    momentum_72 = (frame["close"] - frame["close"].shift(72)) / atr
    merged = pd.merge_asof(
        frame[["open_time"]].sort_values("open_time"),
        market.sort_values("open_time"), on="open_time", direction="backward",
    )
    eligible = np.zeros(len(frame), dtype=np.int8)
    selected = universe.loc[(universe["symbol"] == symbol) & universe["rank"].le(15)]
    for row in selected.itertuples():
        mask = (frame["open_time"] >= row.effective_from) & (frame["open_time"] < row.effective_to)
        eligible[mask.to_numpy()] = int(bool(row.eligible))
    funding_by_time = dict(zip(
        pd.to_datetime(funding["available_at"], unit="ms", utc=True).astype("int64") // 1_000_000,
        funding["funding_rate"].astype(float),
    ))
    times = frame["open_time"].astype("int64").to_numpy() // 1_000_000
    funding_rates = np.fromiter((funding_by_time.get(int(t), 0.0) for t in times), dtype=np.float64)
    return (
        times.astype(np.int64), frame["open"].to_numpy(float), frame["high"].to_numpy(float),
        frame["low"].to_numpy(float), frame["close"].to_numpy(float),
        psar["sar_direction"].to_numpy(np.int8), psar["sar_reversal"].to_numpy(np.int8),
        atr.to_numpy(float), atr_base.to_numpy(float), adx["adx_1h"].to_numpy(float),
        adx["plus_di_1h"].to_numpy(float), adx["minus_di_1h"].to_numpy(float),
        merged["btc_4h_trend"].fillna(0).to_numpy(np.int8),
        merged["btc_7d_momentum"].fillna(0).to_numpy(np.int8), eligible,
        funding_rates, slow_distance.to_numpy(float), momentum_72.to_numpy(float),
    )


@njit(cache=True)
def _simulate_window(
    data,
    start_ms,
    end_ms,
    lookback,
    atr_limit,
    gate_code,
    entry_style,
    initial_exit_bars=12,
    confirmation_add=0,
):
    (times, opens, highs, lows, closes, sar_dir, reversal, atr, atr_base, adx,
     plus_di, minus_di, btc_trend, btc_mom, eligible, funding, slow_distance,
     momentum_72) = data
    cash = 10000.0 / 15.0
    initial = cash
    direction = 0
    layers = 0
    anchor = 0.0
    layer_qty = 0.0
    entry_cost = 0.0
    entry_notional = 0.0
    funding_trade = 0.0
    armed = False
    opposite_run = 0
    pending_dir = 0
    pending_run = 0
    trades = wins = adds = fills = 0
    long_pnl = short_pnl = fees = funding_pnl = gross_profit = gross_loss = 0.0
    peak = cash
    max_dd = 0.0
    first = np.searchsorted(times, start_ms)
    last = np.searchsorted(times, end_ms)
    for i in range(max(first, lookback + 2), last):
        if direction != 0 and funding[i] != 0.0:
            payment = -direction * layer_qty * layers * opens[i] * funding[i]
            cash += payment
            funding_trade += payment
            funding_pnl += payment
        if direction != 0 and eligible[i] == 0:
            cash, net, fee = _close_position(cash, direction, layers, layer_qty, opens[i], entry_notional, entry_cost, funding_trade)
            fees += fee; fills += 1
            trades += 1
            wins += int(net > 0.0)
            gross_profit += max(net, 0.0); gross_loss += min(net, 0.0)
            if direction > 0: long_pnl += net
            else: short_pnl += net
            direction = 0; layers = 0; armed = False; opposite_run = 0
        if eligible[i] == 0:
            pending_dir = 0; pending_run = 0
        d = i - 1
        if eligible[i] != 0:
            if direction == 0:
                candidate = 0
                if reversal[d] != 0 and np.isfinite(atr_base[d]) and atr[d] >= atr_limit * atr_base[d] and adx[d] <= 35.0:
                    maturity_ok = False
                    if entry_style == 0:
                        prior_high = np.max(highs[d - lookback:d])
                        prior_low = np.min(lows[d - lookback:d])
                        maturity_ok = (sar_dir[d] > 0 and closes[d] > prior_high) or (sar_dir[d] < 0 and closes[d] < prior_low)
                    elif np.isfinite(slow_distance[d]) and np.isfinite(momentum_72[d]):
                        limit = 0.0 if entry_style == 1 else 1.0
                        maturity_ok = sar_dir[d] * slow_distance[d] <= limit and sar_dir[d] * momentum_72[d] <= limit
                    if maturity_ok:
                        candidate = sar_dir[d]
                if candidate != 0:
                    pending_dir = candidate; pending_run = 1
                elif pending_dir != 0:
                    if sar_dir[d] == pending_dir:
                        pending_run += 1
                    else:
                        pending_dir = 0; pending_run = 0
                if pending_dir != 0 and pending_run >= 3:
                    if _gate_allows(pending_dir, gate_code, btc_trend[d], btc_mom[d]):
                        direction = pending_dir
                        fill = opens[i] * (1.0 + direction * 0.0002)
                        layer_qty = cash / fill / 5.0
                        fee = layer_qty * fill * 0.001
                        cash -= fee; fees += fee; entry_cost = fee; entry_notional = layer_qty * fill; funding_trade = 0.0
                        layers = 1; anchor = fill; fills += 1
                    pending_dir = 0; pending_run = 0
            else:
                if sar_dir[d] == -direction: opposite_run += 1
                else: opposite_run = 0
                exit_bars = initial_exit_bars if layers == 1 else 12
                if opposite_run >= exit_bars:
                    cash, net, fee = _close_position(cash, direction, layers, layer_qty, opens[i], entry_notional, entry_cost, funding_trade)
                    fees += fee; fills += 1; trades += 1; wins += int(net > 0.0)
                    gross_profit += max(net, 0.0); gross_loss += min(net, 0.0)
                    if direction > 0: long_pnl += net
                    else: short_pnl += net
                    direction = 0; layers = 0; armed = False; opposite_run = 0
                elif layers < 5 and adx[d] >= 25.0 and ((direction > 0 and plus_di[d] > minus_di[d]) or (direction < 0 and minus_di[d] > plus_di[d])):
                    confirmation_ready = (
                        confirmation_add != 0
                        and layers == 1
                        and d >= 12
                        and adx[d] > adx[d - 12]
                    )
                    if confirmation_ready:
                        fill = opens[i] * (1.0 + direction * 0.0002)
                        progressive = (direction > 0 and fill > anchor) or (direction < 0 and fill < anchor)
                        if progressive:
                            fee = layer_qty * fill * 0.001
                            cash -= fee; fees += fee; entry_cost += fee
                            entry_notional += layer_qty * fill
                            layers += 1; anchor = fill; adds += 1; fills += 1; armed = False
                    elif not armed and ((direction > 0 and closes[d] < anchor) or (direction < 0 and closes[d] > anchor)):
                        armed = True
                    elif armed and ((direction > 0 and closes[d] > anchor * 1.0024) or (direction < 0 and closes[d] < anchor * 0.9976)):
                        fill = opens[i] * (1.0 + direction * 0.0002)
                        progressive = (direction > 0 and fill > anchor) or (direction < 0 and fill < anchor)
                        if progressive:
                            fee = layer_qty * fill * 0.001
                            cash -= fee; fees += fee; entry_cost += fee
                            entry_notional += layer_qty * fill
                            layers += 1; anchor = fill; adds += 1; fills += 1; armed = False
        equity = cash
        if direction != 0:
            equity += direction * (layer_qty * layers * closes[i] - entry_notional)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    if direction != 0:
        cash, net, fee = _close_position(cash, direction, layers, layer_qty, closes[last - 1], entry_notional, entry_cost, funding_trade)
        fees += fee; fills += 1; trades += 1; wins += int(net > 0.0)
        gross_profit += max(net, 0.0); gross_loss += min(net, 0.0)
        if direction > 0: long_pnl += net
        else: short_pnl += net
    return np.array((cash / initial - 1.0, max_dd, trades, wins, adds, fills, fees, funding_pnl, gross_profit, gross_loss, long_pnl, short_pnl))


@njit(cache=True)
def _close_position(cash, direction, layers, quantity, reference, entry_notional, entry_cost, funding_trade):
    fill = reference * (1.0 - direction * 0.0002)
    fee = quantity * layers * fill * 0.001
    gross = direction * (quantity * layers * fill - entry_notional)
    net = gross + funding_trade - entry_cost - fee
    cash += gross - fee
    return cash, net, fee


@njit(cache=True)
def _gate_allows(direction, gate, trend, momentum):
    if gate == 0: return True
    if gate == 1: return direction == trend
    if gate == 2: return direction < 0 or trend > 0
    if gate == 3: return direction < 0
    if gate == 4: return direction == momentum
    return direction == trend and direction == momentum


def _metric_record(values: np.ndarray) -> dict[str, Any]:
    keys = ("return", "max_drawdown", "trades", "wins", "adds", "fills", "fees", "funding_pnl", "gross_profit", "gross_loss", "long_pnl", "short_pnl")
    result = dict(zip(keys, map(float, values)))
    result["trades"] = int(result["trades"]); result["wins"] = int(result["wins"])
    result["adds"] = int(result["adds"]); result["fills"] = int(result["fills"])
    return result


def _portfolio_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    trades = sum(item["trades"] for item in items)
    wins = sum(item["wins"] for item in items)
    profit = sum(item["gross_profit"] for item in items)
    loss = sum(item["gross_loss"] for item in items)
    positive = sum(item["return"] > 0 for item in items)
    return {
        "return": float(np.mean([item["return"] for item in items])),
        "max_symbol_drawdown": float(min(item["max_drawdown"] for item in items)),
        "trades": trades, "fills": sum(item["fills"] for item in items),
        "adds": sum(item["adds"] for item in items),
        "win_rate": wins / trades if trades else 0.0,
        "profit_factor": profit / -loss if loss < 0 else None,
        "fees": sum(item["fees"] for item in items),
        "funding_pnl": sum(item["funding_pnl"] for item in items),
        "long_pnl": sum(item["long_pnl"] for item in items),
        "short_pnl": sum(item["short_pnl"] for item in items),
        "profitable_symbols": positive,
    }


def _candidate_summary(grid: pd.DataFrame, detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, group in grid.groupby(["entry_style", "lookback", "atr_expansion", "market_gate"], sort=False):
        annual = group.loc[group["window"].isin(["2024", "2025"]), "trades"]
        concentration = detail.loc[
            (detail["entry_style"] == keys[0]) & (detail["lookback"] == keys[1]) & (detail["atr_expansion"] == keys[2]) & (detail["market_gate"] == keys[3])
        ].groupby("symbol")["gross_profit"].sum().clip(lower=0)
        rows.append({
            "entry_style": keys[0], "lookback": keys[1], "atr_expansion": keys[2], "market_gate": keys[3],
            "mean_return": group["return"].mean(), "worst_return": group["return"].min(),
            "mean_annual_trades": annual.mean(), "minimum_trades": group["trades"].min(),
            "profitable_windows": int(group["return"].gt(0).sum()),
            "minimum_profit_factor": group["profit_factor"].min(),
            "max_symbol_drawdown": group["max_symbol_drawdown"].min(),
            "top_profit_symbol_share": concentration.max() / concentration.sum() if concentration.sum() else 0.0,
        })
    return pd.DataFrame(rows).sort_values(
        ["profitable_windows", "worst_return", "mean_return"], ascending=[False, False, False]
    ).reset_index(drop=True)


def _admission(candidates: pd.DataFrame) -> dict[str, Any]:
    passed = candidates.loc[(candidates["profitable_windows"] == 3) & (candidates["mean_annual_trades"] >= 600)]
    return {
        "passed": not passed.empty,
        "production_changed": False,
        "reason": "candidate_requires_independent_Backtrader_verification" if not passed.empty else "no_gate_passed_all_windows_and_frequency_requirement",
    }


def _bars_with_time(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)


def _ms(value: str) -> int:
    return int(pd.Timestamp(value, tz="UTC").timestamp() * 1000)


def _json_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(frame.to_json(orient="records"))


def _evidence(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
