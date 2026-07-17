"""Build causal, cost-aware terminal-return labels for trend decisions."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.datastore import LABELS_DIR, PARQUET_DIR

HORIZON_BARS = {"30m": 6, "1h": 12, "4h": 48}


def build_symbol(
    symbol: str,
    *,
    variant: str = "trend_v1",
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0002,
    volatility_multiplier: float = 0.5,
) -> list[dict]:
    source = PARQUET_DIR / f"{symbol}_5m.parquet"
    bars = pd.read_parquet(source).sort_values("open_time").reset_index(drop=True)
    if bars["open_time"].dtype.kind == "i":
        bars["open_time"] = pd.to_datetime(bars["open_time"], unit="ms")

    log_return = np.log(bars["close"] / bars["close"].shift(1))
    sigma_5m = log_return.rolling(288, min_periods=144).std()
    round_trip_cost = 2.0 * (fee_rate + slippage_rate)
    reports = []

    for horizon, horizon_bars in HORIZON_BARS.items():
        entry = bars["open"].shift(-1)
        exit_price = bars["close"].shift(-horizon_bars)
        forward_return = exit_price / entry - 1.0
        horizon_sigma = sigma_5m * np.sqrt(horizon_bars)
        threshold = np.maximum(round_trip_cost, volatility_multiplier * horizon_sigma)
        valid = entry.notna() & exit_price.notna() & horizon_sigma.notna()

        long_label = ((forward_return > threshold) & valid).astype(np.int8)
        short_label = ((forward_return < -threshold) & valid).astype(np.int8)
        trend_class = long_label - short_label
        trend_score = (forward_return / horizon_sigma.replace(0.0, np.nan)).clip(-5.0, 5.0)

        output = pd.DataFrame({
            "open_time": bars["open_time"],
            "forward_return": forward_return.astype(np.float32),
            "horizon_sigma": horizon_sigma.astype(np.float32),
            "decision_threshold": pd.Series(threshold).astype(np.float32),
            "long_net_return": (forward_return - round_trip_cost).astype(np.float32),
            "short_net_return": (-forward_return - round_trip_cost).astype(np.float32),
            "long_label": long_label,
            "short_label": short_label,
            "long_meta_label": long_label,
            "short_meta_label": short_label,
            "long_profit_r": trend_score.astype(np.float32),
            "short_profit_r": (-trend_score).astype(np.float32),
            "long_duration_bars": np.full(len(bars), horizon_bars, dtype=np.int16),
            "short_duration_bars": np.full(len(bars), horizon_bars, dtype=np.int16),
            "long_barrier_hit": np.where(long_label == 1, "terminal_profit", "terminal_other"),
            "short_barrier_hit": np.where(short_label == 1, "terminal_profit", "terminal_other"),
            "trend_class": trend_class.astype(np.int8),
            "trend_score": trend_score.astype(np.float32),
            "label_valid": valid.astype(np.int8),
        })
        output_path = LABELS_DIR / f"{symbol}_{horizon}_{variant}_labels.parquet"
        output.to_parquet(output_path, index=False)
        reports.append({
            "symbol": symbol,
            "horizon": horizon,
            "rows": len(output),
            "valid_rows": int(valid.sum()),
            "long_rate": float(long_label[valid].mean()),
            "short_rate": float(short_label[valid].mean()),
            "median_sigma": float(horizon_sigma[valid].median()),
            "round_trip_cost": round_trip_cost,
            "output": str(output_path),
        })
    return reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--variant", default="trend_v1")
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    parser.add_argument("--volatility-multiplier", type=float, default=0.5)
    args = parser.parse_args()

    symbols = (
        ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        if args.symbol == "all"
        else [args.symbol]
    )
    for symbol in symbols:
        for report in build_symbol(
            symbol,
            variant=args.variant,
            fee_rate=args.fee_rate,
            slippage_rate=args.slippage_rate,
            volatility_multiplier=args.volatility_multiplier,
        ):
            print(report)


if __name__ == "__main__":
    main()
