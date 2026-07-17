"""Evaluate a saved RL PPO model on historical bars.

Examples:
    python -m backend.scripts.evaluation.rl_eval --model MODEL.zip --symbol BTCUSDT --start 2024-06-01 --end 2024-08-01 --sanitize
    python -m backend.scripts.evaluation.rl_eval --model MODEL.zip --symbol BTCUSDT --start 2024-06-01 --end 2024-08-01 --sanitize --compare-baseline --json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.config import RLConfig
from backend.app.rl.data import load_ml_scored_bars, select_feature_columns
from backend.app.rl.evaluate import threshold_policy
from backend.app.rl.metrics import evaluate_policy_detailed
from backend.app.rl.model_io import load_policy_model, predict_action


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--sanitize", action="store_true", help="Convert invalid trade actions to HOLD during evaluation")
    parser.add_argument("--compare-baseline", action="store_true", help="Evaluate threshold baseline on the same window")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    bars = load_ml_scored_bars(args.symbol, start=args.start, end=args.end)
    config = RLConfig(feature_columns=select_feature_columns(bars))
    model = load_policy_model(args.model)

    def policy(obs, info):
        return predict_action(model, obs, info, sanitize=args.sanitize, use_mask=True)

    report = {
        "symbol": args.symbol,
        "start": args.start,
        "end": args.end,
        "model": str(args.model),
        "sanitize": bool(args.sanitize),
        "ppo": evaluate_policy_detailed(bars, policy, config=config).to_dict(),
    }
    if args.compare_baseline:
        baseline = evaluate_policy_detailed(
            bars,
            threshold_policy(config.reward.opportunity_threshold, config.reward.opportunity_threshold),
            config=config,
        )
        report["baseline"] = baseline.to_dict()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)


def print_report(report: dict) -> None:
    print(f"symbol={report['symbol']} window={report['start']}..{report['end']} sanitize={report['sanitize']}")
    for name in ("baseline", "ppo"):
        if name not in report:
            continue
        block = report[name]
        summary = block["summary"]
        trades = block["trade_stats"]
        print(f"\n[{name}]")
        print(
            "summary "
            f"final_equity={summary['final_equity']:.6f} "
            f"max_dd={summary['max_drawdown']:.4f} "
            f"reward={summary['total_reward']:.4f} "
            f"invalid={summary['invalid_actions']} "
            f"actions={summary['action_counts']}"
        )
        print(
            "trades  "
            f"n={trades['trades']} long={trades['long_trades']} short={trades['short_trades']} "
            f"win_rate={trades['win_rate']:.4f} avg_ret={trades['avg_return_pct']:.4f} "
            f"pf={trades['profit_factor']:.4f} avg_bars={trades['avg_bars_held']:.2f} "
            f"best={trades['best_trade_pct']:.4f} worst={trades['worst_trade_pct']:.4f}"
        )


if __name__ == "__main__":
    main()
