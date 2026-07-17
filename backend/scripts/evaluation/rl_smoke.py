"""Run RL environment smoke checks.

Examples:
    python -m backend.scripts.evaluation.rl_smoke
    python -m backend.scripts.evaluation.rl_smoke --symbol BTCUSDT --start 2024-01-01 --end 2024-03-01
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.config import RLConfig
from backend.app.rl.data import load_ml_scored_bars, select_feature_columns
from backend.app.rl.evaluate import evaluate_policy, make_synthetic_bars, threshold_policy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="Optional symbol to load from existing ML scored bars")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--long-threshold", type=float, default=0.62)
    parser.add_argument("--short-threshold", type=float, default=0.62)
    args = parser.parse_args()

    if args.symbol:
        bars = load_ml_scored_bars(args.symbol, start=args.start, end=args.end)
        feature_columns = select_feature_columns(bars)
    else:
        bars = make_synthetic_bars()
        feature_columns = ()

    result = evaluate_policy(
        bars,
        threshold_policy(args.long_threshold, args.short_threshold),
        RLConfig(feature_columns=feature_columns),
    )
    print(result)


if __name__ == "__main__":
    main()
