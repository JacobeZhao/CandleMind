"""Aggregate matching walk-forward reports across PPO random seeds."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.input]
    result = aggregate_seed_reports(reports, source_paths=args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


def aggregate_seed_reports(reports: list[dict], *, source_paths: list[Path] | None = None) -> dict:
    if not reports:
        raise ValueError("At least one report is required")
    expected_windows = _windows(reports[0])
    expected_config = _config_signature(reports[0])
    for report in reports[1:]:
        if _windows(report) != expected_windows:
            raise ValueError("Reports use different walk-forward windows")
        if _config_signature(report) != expected_config:
            raise ValueError("Reports use different training configurations")

    seeds = []
    fold_results = []
    all_equities: list[float] = []
    for report in reports:
        seed = _report_seed(report)
        equities = [float(fold["ppo"]["summary"]["final_equity"]) for fold in report["folds"]]
        all_equities.extend(equities)
        seeds.append(
            {
                "seed": seed,
                "mean_equity": statistics.fmean(equities),
                "worst_equity": min(equities),
                "gate_decision": report["promotion_gate"]["decision"],
            }
        )

    median_wins = 0
    for index, window in enumerate(expected_windows):
        equities = [float(report["folds"][index]["ppo"]["summary"]["final_equity"]) for report in reports]
        drawdowns = [float(report["folds"][index]["ppo"]["summary"]["max_drawdown"]) for report in reports]
        trade_counts = [int(report["folds"][index]["ppo"]["trade_stats"]["trades"]) for report in reports]
        first_fold = reports[0]["folds"][index]
        comparator_equities = [float(first_fold["baseline"]["summary"]["final_equity"])]
        comparator_equities.extend(
            float(item["summary"]["final_equity"])
            for item in first_fold.get("comparators", {}).values()
        )
        best_comparator = max(comparator_equities)
        median_equity = statistics.median(equities)
        median_wins += int(median_equity > best_comparator)
        fold_results.append(
            {
                "fold": index + 1,
                "window": window,
                "equities_by_seed": equities,
                "median_equity": median_equity,
                "worst_equity": min(equities),
                "worst_drawdown": max(drawdowns),
                "median_trades": statistics.median(trade_counts),
                "best_comparator_equity": best_comparator,
                "median_beats_best_comparator": median_equity > best_comparator,
            }
        )

    status = "candidate" if median_wins >= 2 and all(item["gate_decision"] == "pass" for item in seeds) else "rejected"
    return {
        "experiment": "BTCUSDT_market_v2_1h_next_open_v2",
        "candidate_status": status,
        "rejection_reasons": [] if status == "candidate" else [
            "median_ppo_did_not_beat_best_comparator_in_2_of_3_folds",
            "all_seed_promotion_gates_failed",
        ],
        "config": expected_config,
        "source_reports": [str(path) for path in source_paths or []],
        "seeds": seeds,
        "folds": fold_results,
        "overall": {
            "runs": len(all_equities),
            "mean_equity": statistics.fmean(all_equities),
            "median_equity": statistics.median(all_equities),
            "profitable_runs": sum(equity > 1.0 for equity in all_equities),
            "median_wins_vs_best_comparator": median_wins,
        },
    }


def _windows(report: dict) -> list[dict]:
    return [
        {"train": fold["train_window"], "test": fold["test_window"]}
        for fold in report["folds"]
    ]


def _config_signature(report: dict) -> dict:
    keys = (
        "symbol", "timesteps", "pretrain_epochs", "target_position",
        "max_position_bars", "fee_rate", "slippage_rate", "position_fraction",
        "decision_interval_bars", "funding_rate_8h", "discount_half_life_hours",
        "feature_set", "min_position_bars", "cooldown_bars",
    )
    return {key: report.get(key) for key in keys}


def _report_seed(report: dict) -> int:
    manifests = [fold["train_result"]["manifest_path"] for fold in report["folds"]]
    for seed in range(10_000):
        if all(f"seed{seed}_" in manifest for manifest in manifests):
            return seed
    raise ValueError("Could not infer seed from model manifests")


if __name__ == "__main__":
    main()
