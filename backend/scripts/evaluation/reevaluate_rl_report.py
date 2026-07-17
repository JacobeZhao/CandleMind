"""Re-evaluate saved walk-forward models after environment cost fixes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.experiment import evaluate_walk_forward_gate
from backend.scripts.training.rl_walk_forward import aggregate_reports, evaluate_fold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    original = json.loads(args.input.read_text(encoding="utf-8"))
    reports = []
    for fold in original["folds"]:
        reports.append(evaluate_fold(
            symbol=original["symbol"],
            model_path=Path(fold["model_path"]),
            train_start=fold["train_window"]["start"],
            train_end=fold["train_window"]["end"],
            test_start=fold["test_window"]["start"],
            test_end=fold["test_window"]["end"],
            target_position=original["target_position"],
            max_position_bars=original["max_position_bars"],
            position_hold_penalty=original["position_hold_penalty"],
            directional_exposure_penalty=original["directional_exposure_penalty"],
            fee_rate=original["eval_fee_rate"],
            slippage_rate=original["eval_slippage_rate"],
            position_fraction=original.get("position_fraction", 0.5),
            decision_interval_bars=original.get("decision_interval_bars", 1),
            funding_rate_8h=original.get("funding_rate_8h", 0.0),
            feature_set=original["feature_set"],
            trend_follow_mode=original["trend_follow_mode"],
            min_position_bars=original["min_position_bars"],
            cooldown_bars=original["cooldown_bars"],
            trend_min_gap=original.get("trend_min_gap", 0.06),
            trend_min_confidence=original.get("trend_min_confidence", 0.52),
            trend_min_hurst=original.get("trend_min_hurst", 0.50),
            trend_max_vol_regime=original.get("trend_max_vol_regime", 2.0),
            trend_monthly_tolerance=original.get("trend_monthly_tolerance", 0.03),
        ))
    result = {**original, "folds": reports, "environment_revision": "next_open_costed_liquidation_v2"}
    result["aggregate"] = aggregate_reports(reports)
    result["promotion_gate"] = evaluate_walk_forward_gate(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "aggregate": result["aggregate"],
        "promotion_gate": result["promotion_gate"],
    }, indent=2))


if __name__ == "__main__":
    main()
