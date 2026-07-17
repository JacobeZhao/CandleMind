"""Stress BTC-trained walk-forward RL models on other symbols and costs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.scripts.training.rl_walk_forward import evaluate_fold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbols", nargs="+", default=["ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"])
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))

    scenarios = {
        "base": {"fee_rate": 0.0010, "slippage_rate": 0.0002},
        "high_cost": {"fee_rate": 0.0015, "slippage_rate": 0.0005},
    }
    results = {}
    for symbol in args.symbols:
        results[symbol] = {}
        for scenario, costs in scenarios.items():
            folds = []
            for fold in source["folds"]:
                folds.append(evaluate_fold(
                    symbol=symbol,
                    model_path=Path(fold["model_path"]),
                    train_start=fold["train_window"]["start"],
                    train_end=fold["train_window"]["end"],
                    test_start=fold["test_window"]["start"],
                    test_end=fold["test_window"]["end"],
                    target_position=source["target_position"],
                    max_position_bars=source["max_position_bars"],
                    position_hold_penalty=source["position_hold_penalty"],
                    directional_exposure_penalty=source["directional_exposure_penalty"],
                    fee_rate=costs["fee_rate"],
                    slippage_rate=costs["slippage_rate"],
                    position_fraction=source.get("position_fraction", 0.5),
                    decision_interval_bars=source.get("decision_interval_bars", 1),
                    funding_rate_8h=source.get("funding_rate_8h", 0.0),
                    feature_set=source["feature_set"],
                    trend_follow_mode=source["trend_follow_mode"],
                    min_position_bars=source["min_position_bars"],
                    cooldown_bars=source["cooldown_bars"],
                    trend_min_gap=source.get("trend_min_gap", 0.06),
                    trend_min_confidence=source.get("trend_min_confidence", 0.52),
                    trend_min_hurst=source.get("trend_min_hurst", 0.50),
                    trend_max_vol_regime=source.get("trend_max_vol_regime", 2.0),
                    trend_monthly_tolerance=source.get("trend_monthly_tolerance", 0.03),
                ))
            equities = [fold["ppo"]["summary"]["final_equity"] for fold in folds]
            results[symbol][scenario] = {
                "mean_equity": sum(equities) / len(equities),
                "profitable_folds": sum(equity > 1.0 for equity in equities),
                "folds": folds,
            }

    report = {
        "source": str(args.input),
        "source_symbol": source["symbol"],
        "symbols": args.symbols,
        "scenarios": scenarios,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        symbol: {
            scenario: {
                "mean_equity": values["mean_equity"],
                "profitable_folds": values["profitable_folds"],
            }
            for scenario, values in scenarios_result.items()
        }
        for symbol, scenarios_result in results.items()
    }
    print(json.dumps({"output": str(args.output), "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
