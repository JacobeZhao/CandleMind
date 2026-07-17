import copy
import json
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.app.rl.experiment import evaluate_walk_forward_gate
from backend.app.rl.config import RLConfig, RewardConfig
from backend.app.rl.feature_engineering import (
    FEATURE_SET_MARKET_V2,
    FEATURE_SET_PROB_V2,
    FEATURE_SET_TREND_FOLLOW_V1,
    FEATURE_SET_V1,
    build_decision_frame,
    build_feature_frame,
)
from backend.app.rl.target_env import TargetPosition, TargetPositionEnv
from backend.app.rl.train import gamma_from_half_life, train_ppo
from backend.scripts.training import rl_walk_forward
from backend.scripts.training.rl_walk_forward import (
    IN_SAMPLE_PROBABILITY_GATE_REASON,
    build_argument_parser,
    evaluate_fold,
    evaluate_walk_forward_promotion_gate,
    load_feature_context,
    validate_windows,
)
from backend.scripts.evaluation.audit_rl_decision_timing import audit_timing
from backend.scripts.evaluation.rl_alpha_baseline import (
    forward_open_return,
    moving_block_bootstrap_mean_ci,
    simulate_non_overlapping,
)
from backend.scripts.evaluation.rl_nested_momentum import _inner_windows, simulate_momentum
from backend.scripts.evaluation.rl_cost_hurdle_alpha import fit_tail_rule, simulate_tail_rule
from backend.scripts.evaluation.audit_rl_microstructure_features import benjamini_hochberg


def _market_bars(periods: int = 10_100) -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="5min")
    close = 100.0 + np.arange(periods, dtype=float) * 0.001
    return pd.DataFrame(
        {
            "open_time": timestamps,
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": np.linspace(1.0, 2.0, periods),
        }
    )


def _evaluation(equity: float, *, trades: int = 10) -> dict:
    return {
        "summary": {
            "final_equity": equity,
            "total_reward": equity - 1.0,
            "max_drawdown": 0.05,
            "invalid_actions": 0,
            "steps": 100,
            "action_counts": {"short": 30, "flat": 40, "long": 30},
        },
        "trade_stats": {
            "trades": trades,
            "long_trades": trades // 2,
            "short_trades": trades - trades // 2,
            "win_rate": 0.6,
            "avg_return_pct": 0.01,
            "median_return_pct": 0.01,
            "gross_profit_pct": 0.15,
            "gross_loss_pct": -0.05,
            "profit_factor": 3.0,
            "avg_bars_held": 12.0,
            "best_trade_pct": 0.04,
            "worst_trade_pct": -0.02,
        },
    }


def _promotion_report() -> dict:
    provenance = rl_walk_forward.probability_provenance(FEATURE_SET_MARKET_V2)
    windows = [
        ("2024-01-01", "2024-02-01", "2024-02-01", "2024-03-01"),
        ("2024-01-01", "2024-03-01", "2024-03-01", "2024-04-01"),
        ("2024-01-01", "2024-04-01", "2024-04-01", "2024-05-01"),
    ]
    folds = []
    for train_start, train_end, test_start, test_end in windows:
        folds.append(
            {
                "train_window": {"start": train_start, "end": train_end},
                "test_window": {"start": test_start, "end": test_end},
                "feature_set": FEATURE_SET_MARKET_V2,
                "probability_provenance": copy.deepcopy(provenance),
                "baseline": _evaluation(1.0),
                "comparators": {},
                "ppo": _evaluation(1.25),
            }
        )
    return {
        "feature_set": FEATURE_SET_MARKET_V2,
        "probability_provenance": provenance,
        "folds": folds,
    }


def _assert_gate_failed(report: object, reason_fragment: str) -> dict:
    gate = evaluate_walk_forward_gate(report)  # type: ignore[arg-type]
    assert gate["decision"] == "fail"
    assert gate["decision"] != "needs_review"
    assert gate["reason"]
    assert any(reason_fragment in reason for reason in gate["reasons"])
    return gate


def test_feature_output_uses_half_open_non_overlapping_windows():
    bars = _market_bars(1_000)
    split = bars.loc[500, "open_time"]
    end = bars.loc[900, "open_time"]
    train = build_feature_frame(
        bars,
        feature_set=FEATURE_SET_MARKET_V2,
        output_end=str(split),
    ).bars
    test = build_feature_frame(
        bars,
        feature_set=FEATURE_SET_MARKET_V2,
        output_start=str(split),
        output_end=str(end),
    ).bars
    assert set(train["open_time"]).isdisjoint(set(test["open_time"]))
    assert test.iloc[0]["open_time"] == split
    assert test.iloc[-1]["open_time"] < end


def test_warmup_populates_monthly_feature_at_test_start():
    bars = _market_bars()
    test_start = bars.loc[9_000, "open_time"]
    result = build_feature_frame(
        bars,
        feature_set=FEATURE_SET_MARKET_V2,
        output_start=str(test_start),
    )
    first = result.bars.iloc[0]
    assert first["open_time"] == test_start
    assert first["monthly_sma"] > 0.0
    assert first["monthly_sma_distance"] != 0.0


def test_walk_forward_rejects_train_test_and_test_test_overlap():
    with pytest.raises(ValueError, match="train and test windows overlap"):
        validate_windows([("2024-01-01", "2024-03-02", "2024-03-01", "2024-04-01")])
    with pytest.raises(ValueError, match="previous test window"):
        validate_windows(
            [
                ("2024-01-01", "2024-02-01", "2024-02-01", "2024-04-01"),
                ("2024-01-01", "2024-03-01", "2024-03-01", "2024-05-01"),
            ]
        )


def test_walk_forward_defaults_to_market_features_without_probability_pretraining():
    args = build_argument_parser().parse_args([])
    assert args.feature_set == FEATURE_SET_MARKET_V2
    assert args.pretrain_epochs == 0
    assert not args.allow_in_sample_probabilities


@pytest.mark.parametrize(
    "feature_set",
    [FEATURE_SET_V1, FEATURE_SET_PROB_V2, FEATURE_SET_TREND_FOLLOW_V1],
)
def test_evaluate_fold_rejects_in_sample_probability_features_by_default(feature_set, tmp_path):
    with pytest.raises(ValueError, match="allow_in_sample_probabilities=True"):
        evaluate_fold(
            symbol="BTCUSDT",
            model_path=tmp_path / "missing.zip",
            train_start="2024-01-01",
            train_end="2024-02-01",
            test_start="2024-02-01",
            test_end="2024-03-01",
            feature_set=feature_set,
        )


def test_cli_rejects_probability_features_without_research_override(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["rl_walk_forward.py", "--feature-set", FEATURE_SET_PROB_V2],
    )
    with pytest.raises(ValueError, match="research-only/non-promotable"):
        rl_walk_forward.main()


def test_evaluate_fold_rejects_probability_feature_set_from_model_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rl_walk_forward,
        "load_feature_context",
        lambda model_path, fallback: (FEATURE_SET_PROB_V2, None),
    )
    model_load_attempted = False

    def load_model(model_path):
        nonlocal model_load_attempted
        model_load_attempted = True

    monkeypatch.setattr(rl_walk_forward, "load_policy_model", load_model)
    with pytest.raises(ValueError, match="allow_in_sample_probabilities=True"):
        evaluate_fold(
            symbol="BTCUSDT",
            model_path=tmp_path / "model.zip",
            train_start="2024-01-01",
            train_end="2024-02-01",
            test_start="2024-02-01",
            test_end="2024-03-01",
            feature_set=FEATURE_SET_MARKET_V2,
        )
    assert not model_load_attempted


def test_explicit_in_sample_probability_override_is_research_only_and_cannot_promote(
    monkeypatch,
    tmp_path,
):
    evaluations = iter([_evaluation(1.0), _evaluation(1.25)])
    monkeypatch.setattr(
        rl_walk_forward,
        "load_feature_context",
        lambda model_path, fallback: (FEATURE_SET_PROB_V2, None),
    )
    monkeypatch.setattr(rl_walk_forward, "load_policy_model", lambda model_path: object())
    monkeypatch.setattr(rl_walk_forward, "load_bars_for_feature_set", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        rl_walk_forward,
        "build_feature_frame",
        lambda *args, **kwargs: SimpleNamespace(
            bars=pd.DataFrame(),
            feature_columns=("long_prob", "short_prob"),
        ),
    )
    monkeypatch.setattr(rl_walk_forward, "build_decision_frame", lambda bars, interval: bars)
    monkeypatch.setattr(
        rl_walk_forward,
        "evaluate_policy_detailed",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: next(evaluations)),
    )

    fold = evaluate_fold(
        symbol="BTCUSDT",
        model_path=tmp_path / "model.zip",
        train_start="2024-01-01",
        train_end="2024-02-01",
        test_start="2024-02-01",
        test_end="2024-03-01",
        feature_set=FEATURE_SET_PROB_V2,
        allow_in_sample_probabilities=True,
    )
    provenance = fold["probability_provenance"]
    assert provenance["source"] == "full_sample_supervised_model"
    assert provenance["fit_scope"] == "in_sample"
    assert provenance["research_only"]
    assert not provenance["oos_valid"]
    assert not provenance["promotion_eligible"]

    report = {
        "feature_set": FEATURE_SET_PROB_V2,
        "probability_provenance": provenance,
        "folds": [{**fold, "comparators": {}} for _ in range(3)],
    }
    gate = evaluate_walk_forward_promotion_gate(report)
    assert gate["decision"] == "fail"
    assert IN_SAMPLE_PROBABILITY_GATE_REASON in gate["reasons"]
    assert not gate["probability_provenance_eligible"]


def test_promotion_gate_requires_profit_and_best_comparator_win():
    report = _promotion_report()
    for fold in report["folds"]:
        fold["comparators"] = {
            "buy_hold": _evaluation(1.20),
            "short_hold": _evaluation(0.85),
        }
        fold["ppo"] = _evaluation(1.10)
    gate = evaluate_walk_forward_gate(report)
    assert gate["decision"] == "fail"
    assert "ppo_fold_win_rate_vs_best_comparator_lt_60pct" in gate["reasons"]

    for fold in report["folds"]:
        fold["ppo"] = _evaluation(1.25)
    gate = evaluate_walk_forward_gate(report)
    assert gate["decision"] == "pass"


def test_promotion_gate_passes_only_verified_market_v2_oof_report():
    gate = evaluate_walk_forward_promotion_gate(_promotion_report())
    assert gate["decision"] == "pass"
    assert gate["reason"] is None
    assert gate["reasons"] == []
    assert gate["probability_provenance_eligible"]


@pytest.mark.parametrize("location", ["report", "fold"])
def test_promotion_gate_fails_when_provenance_is_missing(location):
    report = _promotion_report()
    if location == "report":
        report.pop("probability_provenance")
        expected = "probability_provenance_missing"
    else:
        report["folds"][1].pop("probability_provenance")
        expected = "folds[1].probability_provenance_missing"
    gate = _assert_gate_failed(report, expected)
    assert not gate["probability_provenance_eligible"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("feature_set", "future_features", "feature_set_unknown"),
        ("schema", "rl_walk_forward_oof_v999", "schema_unknown"),
        ("feature_schema_version", "rl_obs_v999", "feature_schema_version_unknown"),
    ],
)
def test_promotion_gate_fails_unknown_feature_set_or_schema(field, value, expected):
    report = _promotion_report()
    if field == "feature_set":
        report[field] = value
    else:
        report["probability_provenance"][field] = value
        for fold in report["folds"]:
            fold["probability_provenance"][field] = value
    _assert_gate_failed(report, expected)


def test_promotion_gate_fails_top_level_and_fold_provenance_conflict():
    report = _promotion_report()
    report["folds"][1]["probability_provenance"]["source"] = "unverified_source"
    gate = _assert_gate_failed(report, "provenance_conflicts_with_report")
    assert not gate["probability_provenance_eligible"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_promotion_gate_fails_non_finite_metrics(value):
    report = _promotion_report()
    report["folds"][0]["ppo"]["summary"]["final_equity"] = value
    _assert_gate_failed(report, "non_finite")


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("final_equity", "1.25", "final_equity_invalid_type"),
        ("invalid_actions", False, "invalid_actions_invalid_type"),
        ("steps", 100.0, "steps_invalid_type"),
    ],
)
def test_promotion_gate_fails_illegal_metric_types(field, value, expected):
    report = _promotion_report()
    report["folds"][0]["ppo"]["summary"][field] = value
    _assert_gate_failed(report, expected)


def test_promotion_gate_fails_illegal_container_types_and_internal_exceptions():
    report = _promotion_report()
    report["folds"] = tuple(report["folds"])
    _assert_gate_failed(report, "folds_invalid_type")

    class ExplodingReport(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    gate = _assert_gate_failed(ExplodingReport(), "gate_evaluation_error:RuntimeError")
    assert not gate["probability_provenance_eligible"]


def test_promotion_gate_fails_with_fewer_than_required_folds():
    report = _promotion_report()
    report["folds"] = report["folds"][:2]
    _assert_gate_failed(report, "walk_forward_folds_lt_3")


@pytest.mark.parametrize(
    ("section", "metric"),
    [
        ("summary", "final_equity"),
        ("summary", "action_counts"),
        ("trade_stats", "trades"),
        ("trade_stats", "profit_factor"),
    ],
)
def test_promotion_gate_fails_when_required_metrics_are_missing(section, metric):
    report = _promotion_report()
    report["folds"][0]["ppo"][section].pop(metric)
    _assert_gate_failed(report, f"{metric}_missing")


def test_load_feature_context_rejects_unknown_schema_and_feature_set(tmp_path):
    model_path = tmp_path / "model.zip"
    schema_path = tmp_path / "feature_schema.json"
    schema_path.write_text(
        json.dumps({"version": "rl_obs_v999", "feature_set": FEATURE_SET_MARKET_V2}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown RL feature schema version"):
        load_feature_context(model_path, FEATURE_SET_MARKET_V2)

    schema_path.write_text(
        json.dumps({"version": "rl_obs_v2", "feature_set": "future_features"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown RL feature_set"):
        load_feature_context(model_path, FEATURE_SET_MARKET_V2)


def test_market_v2_rejects_probability_pretraining(tmp_path):
    with pytest.raises(ValueError, match="does not support probability-threshold pretraining"):
        train_ppo(
            symbol="BTCUSDT",
            start="2024-01-01",
            end="2024-02-01",
            timesteps=1,
            output_dir=tmp_path,
            feature_set=FEATURE_SET_MARKET_V2,
            pretrain_epochs=1,
        )


def test_gamma_matches_24_hour_half_life_for_hourly_decisions():
    gamma = gamma_from_half_life(bar_minutes=60, half_life_hours=24.0)
    assert gamma == pytest.approx(math.exp(math.log(0.5) / 24.0))
    assert gamma**24 == pytest.approx(0.5)


def test_decision_frame_executes_immediately_after_completed_interval():
    timestamps = pd.date_range("2024-01-01", periods=6, freq="5min")
    bars = pd.DataFrame(
        {
            "open_time": timestamps,
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 112.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5, 111.0],
            "volume": 1.0,
            "signal": np.arange(6, dtype=float),
        }
    )
    decision_bars = build_decision_frame(bars, 3)

    assert decision_bars["open_time"].tolist() == [timestamps[2], timestamps[5]]
    assert decision_bars["signal"].tolist() == [2.0, 5.0]
    assert decision_bars.iloc[1]["open"] == 103.0
    assert decision_bars.iloc[1]["close"] == 111.0

    config = RLConfig(
        feature_columns=("signal",),
        position_fraction=1.0,
        bar_minutes=15,
        fee_rate=0.0,
        slippage_rate=0.0,
        funding_rate_8h=0.0,
        reward=RewardConfig(probability_shaping=False),
    )
    env = TargetPositionEnv(decision_bars, config=config)
    env.reset()
    _, _, terminated, _, info = env.step(int(TargetPosition.LONG))

    assert terminated
    assert info["execution_price"] == 103.0
    assert info["mark_price"] == 111.0
    assert info["equity"] == pytest.approx(111.0 / 103.0)


def test_decision_frame_rejects_incomplete_history():
    with pytest.raises(ValueError, match="two complete decision intervals"):
        build_decision_frame(_market_bars(11), 12)


def test_timing_audit_exposes_legacy_observation_delay():
    bars = _market_bars(400)
    bars["signal"] = np.arange(len(bars), dtype=float)
    report = audit_timing(
        bars,
        feature_columns=("signal",),
        decision_interval_bars=12,
        base_bar_minutes=5,
        horizon_hours=[1],
    )
    assert report["legacy_observation_lag_minutes"] == 55.0
    assert report["corrected_observation_lag_minutes"] == 0.0


def test_forward_return_uses_next_execution_open_and_fixed_exit():
    decisions = pd.DataFrame({"open": [99.0, 100.0, 101.0, 110.0, 120.0]})
    result = forward_open_return(decisions, 2)
    assert result.iloc[0] == pytest.approx(110.0 / 100.0 - 1.0)
    assert result.iloc[1] == pytest.approx(120.0 / 101.0 - 1.0)


def test_non_overlapping_baseline_charges_both_sides_of_cost():
    frame = pd.DataFrame({"forward_2h": [0.10, -0.20, -0.10, 0.50]})
    result = simulate_non_overlapping(
        frame,
        np.asarray([1.0, 1.0, -1.0, 1.0]),
        horizon_hours=2,
        side_from_score=lambda score: 1 if score > 0 else -1,
        position_fraction=0.5,
        one_way_cost=0.01,
    )
    assert result["trades"] == 2
    assert result["trade_returns"] == pytest.approx([0.04, 0.04])
    assert result["final_equity"] == pytest.approx(1.04**2)


def test_moving_block_bootstrap_is_reproducible():
    values = np.asarray([0.01, -0.005, 0.02, 0.015, -0.002])
    first = moving_block_bootstrap_mean_ci(values, replications=200, seed=9)
    second = moving_block_bootstrap_mean_ci(values, replications=200, seed=9)
    assert first == second


def test_baseline_metrics_are_json_serializable():
    frame = pd.DataFrame({"forward_1h": [0.02, -0.01]})
    result = simulate_non_overlapping(
        frame,
        np.asarray([1.0, -1.0]),
        horizon_hours=1,
        side_from_score=lambda score: 1 if score > 0 else -1,
        position_fraction=0.5,
        one_way_cost=0.001,
    )
    json.dumps(result)


def test_nested_momentum_applies_stride_trend_gate_and_costs():
    frame = pd.DataFrame(
        {
            "1h_ret_6_z": [2.0, 2.0, -2.0, -2.0, 2.0],
            "market_trend_score": [1.0, 1.0, 1.0, -1.0, 1.0],
            "forward_2h": [0.10, 0.10, -0.10, -0.10, 0.10],
        }
    )
    result = simulate_momentum(
        frame,
        {"threshold": 1.5, "hold_hours": 2, "entry_stride_hours": 2, "trend_gate": True},
        position_fraction=0.5,
        one_way_cost=0.01,
    )
    assert result["trades"] == 2
    assert result["long_trades"] == 2
    assert result["trade_returns"] == pytest.approx([0.04, 0.04])


def test_inner_windows_cover_last_eight_months_without_overlap():
    windows = _inner_windows("2025-01-01")
    assert windows == [
        ("2024-05-01", "2024-07-01"),
        ("2024-07-01", "2024-09-01"),
        ("2024-09-01", "2024-11-01"),
        ("2024-11-01", "2025-01-01"),
    ]


def test_benjamini_hochberg_preserves_original_order():
    adjusted = benjamini_hochberg(np.asarray([0.04, 0.001, 0.02]))
    assert adjusted == pytest.approx([0.04, 0.003, 0.03])


def test_cost_hurdle_rule_fits_direction_and_respects_non_overlap():
    periods = 24 * 180
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="1h")
    signal = np.sin(np.arange(periods) / 13.0)
    frame = pd.DataFrame(
        {
            "dt": timestamps,
            "funding_signal": signal,
            "forward_24h": -0.02 * signal,
        }
    )
    candidate = {
        "feature": "funding_signal",
        "horizon_hours": 24,
        "tail_fraction": 0.10,
    }

    rule = fit_tail_rule(frame, candidate, one_way_cost=0.0012)
    assert rule is not None
    assert rule["direction"] == -1
    assert rule["qualified"]

    result = simulate_tail_rule(
        frame,
        rule,
        position_fraction=0.5,
        one_way_cost=0.0012,
    )
    assert result["trades"] <= len(frame) // 24 + 1
    assert result["final_equity"] > 1.0
