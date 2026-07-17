"""Walk-forward training and evaluation for the RL decision layer.

Example quick check:
    python -m backend.scripts.training.rl_walk_forward --symbol BTCUSDT --window 2024-01-01:2024-02-01:2024-02-01:2024-03-01 --timesteps 1000
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.datastore import MARKET_ROOT
from backend.app.rl.config import RLConfig, RewardConfig
from backend.app.rl.data import load_bars_for_feature_set
from backend.app.rl.feature_engineering import (
    FEATURE_SETS,
    FEATURE_SET_MARKET_V2,
    build_decision_frame,
    build_feature_frame,
)
from backend.app.rl.evaluate import threshold_policy
from backend.app.rl.experiment import (
    FEATURE_SCHEMA_VERSION,
    IN_SAMPLE_PROVENANCE_REASON,
    PROMOTION_PROVENANCE_SCHEMA,
    evaluate_walk_forward_gate,
)
from backend.app.rl.metrics import evaluate_policy_detailed
from backend.app.rl.model_io import load_policy_model, predict_action
from backend.app.rl.target_evaluate import evaluate_target_policy_detailed, threshold_target_policy
from backend.app.rl.train import train_ppo

DEFAULT_WINDOWS = [
    "2024-01-01:2024-03-01:2024-03-01:2024-04-01",
    "2024-03-01:2024-06-01:2024-06-01:2024-08-01",
    "2024-06-01:2024-09-01:2024-09-01:2024-10-01",
]
PROBABILITY_FEATURES = frozenset({"long_prob", "short_prob"})
IN_SAMPLE_PROBABILITY_GATE_REASON = IN_SAMPLE_PROVENANCE_REASON


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--window", action="append", help="train_start:train_end:test_start:test_end; may be repeated")
    parser.add_argument("--timesteps", type=int, default=20_000)
    parser.add_argument("--pretrain-epochs", type=int, default=0)
    parser.add_argument("--no-mask-actions", action="store_true", help="Use plain PPO without action masks")
    parser.add_argument("--target-position", action="store_true", help="Use short/flat/long target-position actions")
    parser.add_argument("--max-position-bars", type=int, default=288, help="Force target-position policies flat after this many bars")
    parser.add_argument("--position-hold-penalty", type=float, default=0.0, help="Per-bar reward penalty while non-flat")
    parser.add_argument("--directional-exposure-penalty", type=float, default=0.0, help="Extra age-scaled reward penalty while non-flat")
    parser.add_argument("--fee-rate", type=float, default=0.0010, help="Training fee rate per unit turnover")
    parser.add_argument("--slippage-rate", type=float, default=0.0002, help="Training slippage rate per unit turnover")
    parser.add_argument("--position-fraction", type=float, default=0.5)
    parser.add_argument("--decision-interval-bars", type=int, default=1)
    parser.add_argument("--funding-rate-8h", type=float, default=0.0)
    parser.add_argument("--discount-half-life-hours", type=float, default=24.0)
    parser.add_argument("--eval-fee-rate", type=float, help="Evaluation-only fee rate for stress tests")
    parser.add_argument("--eval-slippage-rate", type=float, help="Evaluation-only slippage rate for stress tests")
    parser.add_argument(
        "--feature-set",
        default=FEATURE_SET_MARKET_V2,
        choices=tuple(FEATURE_SETS),
        help="RL feature set; market_v2 is the walk-forward-safe default",
    )
    parser.add_argument(
        "--allow-in-sample-probabilities",
        action="store_true",
        help="Research only: allow probability features fitted on the full sample; disables promotion",
    )
    parser.add_argument("--trend-follow-mode", action="store_true", help="Constrain target actions to trend-following gates")
    parser.add_argument("--min-position-bars", type=int, default=12, help="Minimum bars to hold while trend remains valid")
    parser.add_argument("--cooldown-bars", type=int, default=12, help="Bars to wait after closing before opening again")
    parser.add_argument("--trend-min-gap", type=float, default=0.06)
    parser.add_argument("--trend-min-confidence", type=float, default=0.52)
    parser.add_argument("--trend-min-hurst", type=float, default=0.50)
    parser.add_argument("--trend-max-vol-regime", type=float, default=2.0)
    parser.add_argument("--trend-monthly-tolerance", type=float, default=0.03)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=MARKET_ROOT / "models" / "rl" / "candidates")
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    probability_provenance(
        args.feature_set,
        allow_in_sample_probabilities=args.allow_in_sample_probabilities,
    )

    eval_fee_rate = args.eval_fee_rate if args.eval_fee_rate is not None else args.fee_rate
    eval_slippage_rate = args.eval_slippage_rate if args.eval_slippage_rate is not None else args.slippage_rate
    windows = [parse_window(w) for w in (args.window or DEFAULT_WINDOWS)]
    validate_windows(windows)
    reports = []
    for idx, (train_start, train_end, test_start, test_end) in enumerate(windows, start=1):
        print(f"\n=== fold {idx}: train={train_start}..{train_end} test={test_start}..{test_end} ===", flush=True)
        fold_dir = args.output_dir / f"fold_{idx:02d}_{train_start}_{train_end}"
        train_result = train_ppo(
            symbol=args.symbol,
            start=train_start,
            end=train_end,
            timesteps=args.timesteps,
            output_dir=fold_dir,
            seed=args.seed,
            pretrain_epochs=args.pretrain_epochs,
            mask_actions=not args.no_mask_actions,
            target_position=args.target_position,
            max_position_bars=args.max_position_bars,
            position_hold_penalty=args.position_hold_penalty,
            directional_exposure_penalty=args.directional_exposure_penalty,
            fee_rate=args.fee_rate,
            slippage_rate=args.slippage_rate,
            position_fraction=args.position_fraction,
            decision_interval_bars=args.decision_interval_bars,
            funding_rate_8h=args.funding_rate_8h,
            discount_half_life_hours=args.discount_half_life_hours,
            feature_set=args.feature_set,
            trend_follow_mode=args.trend_follow_mode,
            min_position_bars=args.min_position_bars,
            cooldown_bars=args.cooldown_bars,
        )
        report = evaluate_fold(
            symbol=args.symbol,
            model_path=train_result.model_path,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            target_position=args.target_position,
            max_position_bars=args.max_position_bars,
            position_hold_penalty=args.position_hold_penalty,
            directional_exposure_penalty=args.directional_exposure_penalty,
            fee_rate=eval_fee_rate,
            slippage_rate=eval_slippage_rate,
            position_fraction=args.position_fraction,
            decision_interval_bars=args.decision_interval_bars,
            funding_rate_8h=args.funding_rate_8h,
            feature_set=args.feature_set,
            trend_follow_mode=args.trend_follow_mode,
            min_position_bars=args.min_position_bars,
            cooldown_bars=args.cooldown_bars,
            trend_min_gap=args.trend_min_gap,
            trend_min_confidence=args.trend_min_confidence,
            trend_min_hurst=args.trend_min_hurst,
            trend_max_vol_regime=args.trend_max_vol_regime,
            trend_monthly_tolerance=args.trend_monthly_tolerance,
            allow_in_sample_probabilities=args.allow_in_sample_probabilities,
        )
        report["train_result"] = {
            "model_path": str(train_result.model_path),
            "run_dir": str(train_result.run_dir),
            "manifest_path": str(train_result.manifest_path),
            "timesteps": train_result.timesteps,
            "pretrain_epochs": train_result.pretrain_epochs,
            "gamma": train_result.gamma,
            "train_eval": train_result.evaluation.__dict__,
        }
        reports.append(report)
        print_fold_summary(report)

    final = {
        "symbol": args.symbol,
        "timesteps": args.timesteps,
        "pretrain_epochs": args.pretrain_epochs,
        "mask_actions": not args.no_mask_actions,
        "target_position": args.target_position,
        "max_position_bars": args.max_position_bars,
        "position_hold_penalty": args.position_hold_penalty,
        "directional_exposure_penalty": args.directional_exposure_penalty,
        "fee_rate": args.fee_rate,
        "slippage_rate": args.slippage_rate,
        "position_fraction": args.position_fraction,
        "decision_interval_bars": args.decision_interval_bars,
        "funding_rate_8h": args.funding_rate_8h,
        "discount_half_life_hours": args.discount_half_life_hours,
        "eval_fee_rate": eval_fee_rate,
        "eval_slippage_rate": eval_slippage_rate,
        "feature_set": args.feature_set,
        "probability_provenance": probability_provenance(
            args.feature_set,
            allow_in_sample_probabilities=args.allow_in_sample_probabilities,
        ),
        "trend_follow_mode": args.trend_follow_mode,
        "min_position_bars": args.min_position_bars,
        "cooldown_bars": args.cooldown_bars,
        "trend_min_gap": args.trend_min_gap,
        "trend_min_confidence": args.trend_min_confidence,
        "trend_min_hurst": args.trend_min_hurst,
        "trend_max_vol_regime": args.trend_max_vol_regime,
        "trend_monthly_tolerance": args.trend_monthly_tolerance,
        "folds": reports,
    }
    final["promotion_gate"] = evaluate_walk_forward_promotion_gate(final)
    aggregate = aggregate_reports(reports)
    final["aggregate"] = aggregate
    print("\n=== aggregate ===")
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    print("gate=" + json.dumps(final["promotion_gate"], sort_keys=True))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(final, indent=2, sort_keys=True), encoding="utf-8")
        print(f"saved={args.json_out}")


def parse_window(raw: str) -> tuple[str, str, str, str]:
    parts = raw.split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid window {raw!r}; expected train_start:train_end:test_start:test_end")
    return tuple(parts)  # type: ignore[return-value]


def validate_windows(windows: list[tuple[str, str, str, str]]) -> None:
    previous_test_end: pd.Timestamp | None = None
    for index, (train_start, train_end, test_start, test_end) in enumerate(windows, start=1):
        train_start_ts = pd.Timestamp(train_start)
        train_end_ts = pd.Timestamp(train_end)
        test_start_ts = pd.Timestamp(test_start)
        test_end_ts = pd.Timestamp(test_end)
        if not train_start_ts < train_end_ts:
            raise ValueError(f"Fold {index} train_start must be before train_end")
        if train_end_ts > test_start_ts:
            raise ValueError(f"Fold {index} train and test windows overlap")
        if not test_start_ts < test_end_ts:
            raise ValueError(f"Fold {index} test_start must be before test_end")
        if previous_test_end is not None and test_start_ts < previous_test_end:
            raise ValueError(f"Fold {index} test window overlaps the previous test window")
        previous_test_end = test_end_ts


def probability_provenance(
    feature_set: str,
    *,
    allow_in_sample_probabilities: bool = False,
) -> dict:
    if not isinstance(feature_set, str) or feature_set not in FEATURE_SETS:
        raise ValueError(f"Unknown RL feature_set {feature_set!r}; available={sorted(FEATURE_SETS)}")
    probability_features = sorted(PROBABILITY_FEATURES.intersection(FEATURE_SETS[feature_set]))
    uses_probability_features = bool(probability_features)
    if uses_probability_features and not allow_in_sample_probabilities:
        features = ", ".join(probability_features)
        raise ValueError(
            f"feature_set {feature_set!r} contains in-sample supervised probability features "
            f"({features}); walk-forward use requires explicit "
            "allow_in_sample_probabilities=True and is research-only/non-promotable"
        )
    return {
        "schema": PROMOTION_PROVENANCE_SCHEMA,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_set": feature_set,
        "uses_probability_features": uses_probability_features,
        "probability_features": probability_features,
        "source": "full_sample_supervised_model" if uses_probability_features else "market_features_only",
        "fit_scope": "in_sample" if uses_probability_features else "out_of_fold",
        "oos_valid": not uses_probability_features,
        "oof_verified": not uses_probability_features,
        "research_only": uses_probability_features,
        "allow_in_sample_probabilities": allow_in_sample_probabilities,
        "promotion_eligible": not uses_probability_features,
    }


def evaluate_walk_forward_promotion_gate(report: dict) -> dict:
    return evaluate_walk_forward_gate(report)


def evaluate_fold(
    *,
    symbol: str,
    model_path: Path,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    target_position: bool = False,
    max_position_bars: int | None = 288,
    position_hold_penalty: float = 0.0,
    directional_exposure_penalty: float = 0.0,
    fee_rate: float = 0.0010,
    slippage_rate: float = 0.0002,
    position_fraction: float = 0.5,
    decision_interval_bars: int = 1,
    funding_rate_8h: float = 0.0,
    feature_set: str = FEATURE_SET_MARKET_V2,
    trend_follow_mode: bool = False,
    min_position_bars: int = 0,
    cooldown_bars: int = 0,
    trend_min_gap: float = 0.06,
    trend_min_confidence: float = 0.52,
    trend_min_hurst: float = 0.50,
    trend_max_vol_regime: float = 2.0,
    trend_monthly_tolerance: float = 0.03,
    allow_in_sample_probabilities: bool = False,
) -> dict:
    probability_provenance(
        feature_set,
        allow_in_sample_probabilities=allow_in_sample_probabilities,
    )
    feature_set, feature_scaler = load_feature_context(model_path, feature_set)
    provenance = probability_provenance(
        feature_set,
        allow_in_sample_probabilities=allow_in_sample_probabilities,
    )
    model = load_policy_model(model_path)
    load_start = test_start
    if feature_set == FEATURE_SET_MARKET_V2:
        load_start = str((pd.Timestamp(test_start) - pd.Timedelta(days=35)).date())
    raw_bars = load_bars_for_feature_set(
        symbol, start=load_start, end=test_end, feature_set=feature_set
    )
    feature_result = build_feature_frame(
        raw_bars,
        feature_set=feature_set,
        scaler=feature_scaler,
        output_start=test_start,
        output_end=test_end,
    )
    bars = build_decision_frame(feature_result.bars, decision_interval_bars)
    reward_config = RewardConfig(
        position_hold_penalty=position_hold_penalty,
        directional_exposure_penalty=directional_exposure_penalty,
        probability_shaping=feature_set != FEATURE_SET_MARKET_V2,
    )
    config = RLConfig(
        feature_columns=feature_result.feature_columns,
        position_fraction=position_fraction,
        bar_minutes=5 * decision_interval_bars,
        max_position_bars=max_position_bars,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        funding_rate_8h=funding_rate_8h,
        min_position_bars=min_position_bars,
        cooldown_bars=cooldown_bars,
        trend_follow_mode=trend_follow_mode,
        trend_min_gap=trend_min_gap,
        trend_min_confidence=trend_min_confidence,
        trend_min_hurst=trend_min_hurst,
        trend_max_vol_regime=trend_max_vol_regime,
        trend_monthly_tolerance=trend_monthly_tolerance,
        reward=reward_config,
    )

    if target_position:
        def ppo_policy(obs, info):
            action, _ = model.predict(obs, deterministic=True)
            return int(action)

        baseline_policy = (
            (lambda obs, info: 1)
            if feature_set == FEATURE_SET_MARKET_V2
            else threshold_target_policy(
                config.reward.opportunity_threshold,
                config.reward.opportunity_threshold,
            )
        )
        baseline = evaluate_target_policy_detailed(bars, baseline_policy, config=config).to_dict()
        ppo = evaluate_target_policy_detailed(bars, ppo_policy, config=config).to_dict()
        hold_config = replace(
            config,
            max_position_bars=None,
            min_position_bars=0,
            cooldown_bars=0,
            trend_follow_mode=False,
        )
        comparators = {
            "buy_hold": evaluate_target_policy_detailed(
                bars, lambda obs, info: 2, config=hold_config
            ).to_dict(),
            "short_hold": evaluate_target_policy_detailed(
                bars, lambda obs, info: 0, config=hold_config
            ).to_dict(),
        }
    else:
        def ppo_policy(obs, info):
            return predict_action(model, obs, info, sanitize=True, use_mask=True)

        baseline = evaluate_policy_detailed(
            bars,
            threshold_policy(config.reward.opportunity_threshold, config.reward.opportunity_threshold),
            config=config,
        ).to_dict()
        ppo = evaluate_policy_detailed(bars, ppo_policy, config=config).to_dict()
    result = {
        "train_window": {"start": train_start, "end": train_end},
        "test_window": {"start": test_start, "end": test_end},
        "model_path": str(model_path),
        "feature_set": feature_set,
        "probability_provenance": provenance,
        "baseline": baseline,
        "ppo": ppo,
    }
    if target_position:
        result["comparators"] = comparators
    return result


def load_feature_context(model_path: Path, fallback_feature_set: str) -> tuple[str, dict | None]:
    run_dir = model_path.parent
    feature_set = fallback_feature_set
    schema_path = run_dir / "feature_schema.json"
    if not schema_path.exists():
        raise ValueError(f"Missing RL feature schema: {schema_path}")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid RL feature schema: {schema_path}") from exc
    if not isinstance(schema, dict):
        raise ValueError(f"Invalid RL feature schema object: {schema_path}")
    schema_version = schema.get("version")
    if schema_version != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            f"Unknown RL feature schema version {schema_version!r}; expected {FEATURE_SCHEMA_VERSION!r}"
        )
    schema_feature_set = schema.get("feature_set")
    if not isinstance(schema_feature_set, str) or schema_feature_set not in FEATURE_SETS:
        raise ValueError(
            f"Unknown RL feature_set {schema_feature_set!r} in {schema_path}; available={sorted(FEATURE_SETS)}"
        )
    feature_set = schema_feature_set
    scaler_path = run_dir / "feature_scaler.json"
    scaler = None
    if scaler_path.exists():
        scaler = json.loads(scaler_path.read_text(encoding="utf-8"))
    return feature_set, scaler

def print_fold_summary(report: dict) -> None:
    base = report["baseline"]["summary"]
    ppo = report["ppo"]["summary"]
    base_trades = report["baseline"]["trade_stats"]
    ppo_trades = report["ppo"]["trade_stats"]
    print(
        "fold_result "
        f"baseline_eq={base['final_equity']:.4f} ppo_eq={ppo['final_equity']:.4f} "
        f"baseline_dd={base['max_drawdown']:.4f} ppo_dd={ppo['max_drawdown']:.4f} "
        f"baseline_trades={base_trades['trades']} ppo_trades={ppo_trades['trades']} "
        f"ppo_invalid={ppo['invalid_actions']}",
        flush=True,
    )


def aggregate_reports(reports: list[dict]) -> dict:
    if not reports:
        return {}
    ppo_eq = [r["ppo"]["summary"]["final_equity"] for r in reports]
    base_eq = [r["baseline"]["summary"]["final_equity"] for r in reports]
    ppo_dd = [r["ppo"]["summary"]["max_drawdown"] for r in reports]
    base_dd = [r["baseline"]["summary"]["max_drawdown"] for r in reports]
    best_comparator_eq = []
    for report, baseline_equity in zip(reports, base_eq):
        equities = [baseline_equity]
        equities.extend(
            comparator["summary"]["final_equity"]
            for comparator in report.get("comparators", {}).values()
        )
        best_comparator_eq.append(max(equities))
    return {
        "folds": len(reports),
        "ppo_mean_equity": sum(ppo_eq) / len(ppo_eq),
        "baseline_mean_equity": sum(base_eq) / len(base_eq),
        "best_comparator_mean_equity": sum(best_comparator_eq) / len(best_comparator_eq),
        "ppo_wins_vs_best_comparator": sum(
            1 for p, comparator in zip(ppo_eq, best_comparator_eq) if p > comparator
        ),
        "ppo_max_drawdown_worst": max(ppo_dd),
        "baseline_max_drawdown_worst": max(base_dd),
        "ppo_invalid_total": sum(r["ppo"]["summary"]["invalid_actions"] for r in reports),
    }


if __name__ == "__main__":
    main()
