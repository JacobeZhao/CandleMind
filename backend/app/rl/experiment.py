"""Experiment manifests and promotion gates for RL research runs."""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from .config import RLConfig
from .feature_engineering import FEATURE_SETS, FEATURE_SET_MARKET_V2

FEATURE_SCHEMA_VERSION = "rl_obs_v2"
PROMOTION_PROVENANCE_SCHEMA = "rl_walk_forward_oof_v1"
IN_SAMPLE_PROVENANCE_REASON = "in_sample_probability_features_not_promotion_eligible"


def make_model_id(
    *,
    algorithm: str,
    symbol: str,
    start: str | None,
    end: str | None,
    seed: int,
    timesteps: int,
) -> str:
    started = _clean_date(start or "all")
    ended = _clean_date(end or "latest")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"rl_{algorithm}_{symbol}_{started}_{ended}_seed{seed}_{timesteps}_{FEATURE_SCHEMA_VERSION}_{stamp}"


def write_train_artifacts(
    *,
    run_dir: Path,
    model_path: Path,
    algorithm: str,
    symbol: str,
    start: str | None,
    end: str | None,
    seed: int,
    timesteps: int,
    pretrain_epochs: int,
    mask_actions: bool,
    config: RLConfig,
    feature_columns: tuple[str, ...],
    row_count: int,
    evaluation: Any,
    data_paths: dict[str, Path],
    feature_set: str = "v1",
    feature_scaler: dict[str, Any] | None = None,
    training_hyperparameters: dict[str, Any] | None = None,
) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    train_config = {
        "algorithm": algorithm,
        "symbol": symbol,
        "train_window": {"start": start, "end": end},
        "seed": seed,
        "timesteps": timesteps,
        "pretrain_epochs": pretrain_epochs,
        "mask_actions": mask_actions,
        "rl_config": asdict(config),
        "feature_set": feature_set,
        "training_hyperparameters": training_hyperparameters or {},
    }
    feature_schema = {
        "version": FEATURE_SCHEMA_VERSION,
        "feature_set": feature_set,
        "columns": list(feature_columns),
        "observation_size": len(feature_columns) + 5,
    }
    eval_summary = _dataclass_to_dict(evaluation)
    manifest = {
        "model_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "status": "candidate",
        "train_config_path": str(run_dir / "train_config.json"),
        "feature_schema_path": str(run_dir / "feature_schema.json"),
        "eval_summary_path": str(run_dir / "eval_summary.json"),
        "feature_scaler_path": str(run_dir / "feature_scaler.json") if feature_scaler is not None else None,
        "git_hash": _git_hash(),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": _package_versions(["stable-baselines3", "sb3-contrib", "gymnasium", "torch", "numpy", "pandas"]),
        "data": {
            "rows": row_count,
            "fingerprint": _data_fingerprint(data_paths),
            "paths": {key: str(value) for key, value in data_paths.items()},
        },
    }
    _write_json(run_dir / "train_config.json", train_config)
    _write_json(run_dir / "feature_schema.json", feature_schema)
    _write_json(run_dir / "eval_summary.json", eval_summary)
    if feature_scaler is not None:
        _write_json(run_dir / "feature_scaler.json", feature_scaler)
    _write_json(run_dir / "manifest.json", manifest)
    return {
        "manifest": run_dir / "manifest.json",
        "train_config": run_dir / "train_config.json",
        "feature_schema": run_dir / "feature_schema.json",
        "eval_summary": run_dir / "eval_summary.json",
        "feature_scaler": run_dir / "feature_scaler.json",
    }


def evaluate_walk_forward_gate(report: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a promotion report without ever failing open.

    Promotion reports are persisted research artifacts, so every value crossing
    this boundary is treated as untrusted structured data.
    """
    try:
        return _evaluate_walk_forward_gate(report)
    except Exception as exc:
        return _gate_result(
            reasons=[f"gate_evaluation_error:{type(exc).__name__}"],
            fold_count=0,
        )


def _evaluate_walk_forward_gate(report: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(report, dict):
        return _gate_result(reasons=["report_invalid_type"], fold_count=0)

    _reject_non_finite_values(report, "report", reasons)

    raw_folds = report.get("folds")
    if not isinstance(raw_folds, list):
        reasons.append("folds_missing" if raw_folds is None else "folds_invalid_type")
        folds: list[Any] = []
    else:
        folds = raw_folds
    if len(folds) < 3:
        reasons.append("walk_forward_folds_lt_3")

    feature_set = report.get("feature_set")
    if feature_set is None:
        reasons.append("feature_set_missing")
    elif not isinstance(feature_set, str):
        reasons.append("feature_set_invalid_type")
    elif feature_set not in FEATURE_SETS:
        reasons.append("feature_set_unknown")
    elif feature_set != FEATURE_SET_MARKET_V2:
        reasons.append("feature_set_not_promotion_eligible")

    top_provenance = report.get("probability_provenance")
    top_provenance_eligible = _validate_promotion_provenance(
        top_provenance,
        path="probability_provenance",
        reasons=reasons,
    )

    ppo_wins = 0
    ppo_profitable = 0
    ppo_invalid = 0
    failed_behavior = 0
    valid_metric_folds = 0
    previous_test_end: datetime | None = None
    provenance_eligible = top_provenance_eligible
    for index, fold in enumerate(folds):
        path = f"folds[{index}]"
        if not isinstance(fold, dict):
            reasons.append(f"{path}_invalid_type")
            provenance_eligible = False
            continue

        fold_provenance = fold.get("probability_provenance")
        fold_provenance_eligible = _validate_promotion_provenance(
            fold_provenance,
            path=f"{path}.probability_provenance",
            reasons=reasons,
        )
        provenance_eligible = provenance_eligible and fold_provenance_eligible
        if isinstance(top_provenance, dict) and isinstance(fold_provenance, dict):
            if _provenance_signature(top_provenance) != _provenance_signature(fold_provenance):
                reasons.append(f"{path}_provenance_conflicts_with_report")
                provenance_eligible = False

        fold_feature_set = fold.get("feature_set")
        if fold_feature_set != feature_set:
            reasons.append(f"{path}_feature_set_conflicts_with_report")
            provenance_eligible = False

        test_window = _validate_fold_windows(fold, path, reasons)
        if test_window is not None:
            test_start, test_end = test_window
            if previous_test_end is not None and test_start < previous_test_end:
                reasons.append(f"{path}_test_window_overlaps_previous_fold")
            previous_test_end = test_end

        baseline = _validate_evaluation(fold.get("baseline"), f"{path}.baseline", reasons)
        ppo = _validate_evaluation(fold.get("ppo"), f"{path}.ppo", reasons)
        raw_comparators = fold.get("comparators", {})
        comparators: list[tuple[str, tuple[dict[str, Any], dict[str, Any]]]] = []
        if not isinstance(raw_comparators, dict):
            reasons.append(f"{path}.comparators_invalid_type")
        else:
            for name, comparator in raw_comparators.items():
                validated = _validate_evaluation(comparator, f"{path}.comparators[{name!r}]", reasons)
                if validated is not None:
                    comparators.append((str(name), validated))

        if baseline is None or ppo is None or not isinstance(raw_comparators, dict):
            continue

        bsum, btr = baseline
        psum, ptr = ppo
        comparator_equities = [float(bsum["final_equity"])]
        comparator_equities.extend(float(summary["final_equity"]) for _, (summary, _) in comparators)
        best_comparator = max(comparator_equities)
        fold["best_comparator_equity"] = best_comparator
        if float(psum["final_equity"]) > best_comparator:
            ppo_wins += 1
        if float(psum["final_equity"]) > 1.0:
            ppo_profitable += 1
        ppo_invalid += int(psum["invalid_actions"])
        behavior = behavior_health(psum, ptr, btr)
        fold["behavior_health"] = behavior
        if behavior["decision"] != "pass":
            failed_behavior += 1
        valid_metric_folds += 1

    win_rate = ppo_wins / len(folds) if folds else 0.0
    if len(folds) >= 3 and valid_metric_folds == len(folds) and win_rate < 0.60:
        reasons.append("ppo_fold_win_rate_vs_best_comparator_lt_60pct")
    profitable_rate = ppo_profitable / len(folds) if folds else 0.0
    if len(folds) >= 3 and valid_metric_folds == len(folds) and profitable_rate < 0.60:
        reasons.append("ppo_profitable_fold_rate_lt_60pct")
    if ppo_invalid != 0:
        reasons.append("invalid_actions_nonzero")
    if failed_behavior:
        reasons.append("behavior_health_failed")

    return _gate_result(
        reasons=reasons,
        fold_count=len(folds),
        ppo_wins=ppo_wins,
        ppo_win_rate=win_rate,
        ppo_profitable=ppo_profitable,
        ppo_profitable_rate=profitable_rate,
        ppo_invalid=ppo_invalid,
        failed_behavior=failed_behavior,
        provenance_eligible=provenance_eligible and len(folds) >= 3,
    )


def _gate_result(
    *,
    reasons: list[str],
    fold_count: int,
    ppo_wins: int = 0,
    ppo_win_rate: float = 0.0,
    ppo_profitable: int = 0,
    ppo_profitable_rate: float = 0.0,
    ppo_invalid: int = 0,
    failed_behavior: int = 0,
    provenance_eligible: bool = False,
) -> dict[str, Any]:
    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "decision": "pass" if not unique_reasons else "fail",
        "reason": unique_reasons[0] if unique_reasons else None,
        "reasons": unique_reasons,
        "folds": fold_count,
        "ppo_wins_vs_best_comparator": ppo_wins,
        "ppo_win_rate": ppo_win_rate,
        "ppo_profitable_folds": ppo_profitable,
        "ppo_profitable_rate": ppo_profitable_rate,
        "ppo_invalid_total": ppo_invalid,
        "failed_behavior_folds": failed_behavior,
        "probability_provenance_eligible": provenance_eligible and not unique_reasons,
    }


def _validate_promotion_provenance(value: Any, *, path: str, reasons: list[str]) -> bool:
    if value is None:
        reasons.append(f"{path}_missing")
        return False
    if not isinstance(value, dict):
        reasons.append(f"{path}_invalid_type")
        return False

    eligible = True
    schema = value.get("schema")
    if schema is None:
        reasons.append(f"{path}.schema_missing")
        eligible = False
    elif not isinstance(schema, str):
        reasons.append(f"{path}.schema_invalid_type")
        eligible = False
    elif schema != PROMOTION_PROVENANCE_SCHEMA:
        reasons.append(f"{path}.schema_unknown")
        eligible = False

    feature_schema = value.get("feature_schema_version")
    if feature_schema is None:
        reasons.append(f"{path}.feature_schema_version_missing")
        eligible = False
    elif not isinstance(feature_schema, str):
        reasons.append(f"{path}.feature_schema_version_invalid_type")
        eligible = False
    elif feature_schema != FEATURE_SCHEMA_VERSION:
        reasons.append(f"{path}.feature_schema_version_unknown")
        eligible = False

    feature_set = value.get("feature_set")
    if feature_set is None:
        reasons.append(f"{path}.feature_set_missing")
        eligible = False
    elif not isinstance(feature_set, str):
        reasons.append(f"{path}.feature_set_invalid_type")
        eligible = False
    elif feature_set not in FEATURE_SETS:
        reasons.append(f"{path}.feature_set_unknown")
        eligible = False

    research_only = (
        value.get("uses_probability_features") is True
        or value.get("fit_scope") == "in_sample"
        or value.get("research_only") is True
        or value.get("oos_valid") is False
        or value.get("promotion_eligible") is False
    )
    if research_only:
        reasons.append(IN_SAMPLE_PROVENANCE_REASON)
        return False

    expected = {
        "feature_set": FEATURE_SET_MARKET_V2,
        "uses_probability_features": False,
        "probability_features": [],
        "source": "market_features_only",
        "fit_scope": "out_of_fold",
        "oos_valid": True,
        "oof_verified": True,
        "research_only": False,
        "allow_in_sample_probabilities": False,
        "promotion_eligible": True,
    }
    for key, expected_value in expected.items():
        if key not in value:
            reasons.append(f"{path}.{key}_missing")
            eligible = False
        elif value[key] != expected_value or type(value[key]) is not type(expected_value):
            reasons.append(f"{path}.{key}_invalid")
            eligible = False
    return eligible


def _provenance_signature(value: dict[str, Any]) -> tuple[Any, ...]:
    keys = (
        "schema",
        "feature_schema_version",
        "feature_set",
        "uses_probability_features",
        "probability_features",
        "source",
        "fit_scope",
        "oos_valid",
        "oof_verified",
        "research_only",
        "allow_in_sample_probabilities",
        "promotion_eligible",
    )
    return tuple(json.dumps(value.get(key), sort_keys=True) for key in keys)


def _validate_fold_windows(
    fold: dict[str, Any],
    path: str,
    reasons: list[str],
) -> tuple[datetime, datetime] | None:
    train = _validate_window(fold.get("train_window"), f"{path}.train_window", reasons)
    test = _validate_window(fold.get("test_window"), f"{path}.test_window", reasons)
    if train is None or test is None:
        return None
    train_start, train_end = train
    test_start, test_end = test
    if train_end > test_start:
        reasons.append(f"{path}_train_test_windows_overlap")
    return test_start, test_end


def _validate_window(value: Any, path: str, reasons: list[str]) -> tuple[datetime, datetime] | None:
    if not isinstance(value, dict):
        reasons.append(f"{path}_missing" if value is None else f"{path}_invalid_type")
        return None
    parsed: list[datetime] = []
    for key in ("start", "end"):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw:
            reasons.append(f"{path}.{key}_invalid")
            return None
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            reasons.append(f"{path}.{key}_invalid")
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        parsed.append(timestamp)
    if parsed[0] >= parsed[1]:
        reasons.append(f"{path}_invalid_order")
        return None
    return parsed[0], parsed[1]


def _validate_evaluation(
    value: Any,
    path: str,
    reasons: list[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not isinstance(value, dict):
        reasons.append(f"{path}_missing" if value is None else f"{path}_invalid_type")
        return None
    summary = value.get("summary")
    trades = value.get("trade_stats")
    if not isinstance(summary, dict):
        reasons.append(f"{path}.summary_missing" if summary is None else f"{path}.summary_invalid_type")
    if not isinstance(trades, dict):
        reasons.append(f"{path}.trade_stats_missing" if trades is None else f"{path}.trade_stats_invalid_type")
    if not isinstance(summary, dict) or not isinstance(trades, dict):
        return None

    valid = True
    valid &= _require_integer(summary, "steps", f"{path}.summary", reasons, minimum=1)
    valid &= _require_number(summary, "final_equity", f"{path}.summary", reasons)
    valid &= _require_number(summary, "total_reward", f"{path}.summary", reasons)
    valid &= _require_number(summary, "max_drawdown", f"{path}.summary", reasons)
    valid &= _require_integer(summary, "invalid_actions", f"{path}.summary", reasons, minimum=0)

    action_counts = summary.get("action_counts")
    if not isinstance(action_counts, dict) or not action_counts:
        reasons.append(f"{path}.summary.action_counts_missing" if action_counts is None else f"{path}.summary.action_counts_invalid_type")
        valid = False
    else:
        if "hold" not in action_counts and "flat" not in action_counts:
            reasons.append(f"{path}.summary.action_counts_hold_or_flat_missing")
            valid = False
        for name in action_counts:
            valid &= _require_integer(action_counts, name, f"{path}.summary.action_counts", reasons, minimum=0)

    for key in ("trades", "long_trades", "short_trades"):
        valid &= _require_integer(trades, key, f"{path}.trade_stats", reasons, minimum=0)
    for key in (
        "win_rate",
        "avg_return_pct",
        "median_return_pct",
        "gross_profit_pct",
        "gross_loss_pct",
        "profit_factor",
        "avg_bars_held",
        "best_trade_pct",
        "worst_trade_pct",
    ):
        valid &= _require_number(trades, key, f"{path}.trade_stats", reasons)
    return (summary, trades) if valid else None


def _require_number(container: dict[str, Any], key: str, path: str, reasons: list[str]) -> bool:
    if key not in container:
        reasons.append(f"{path}.{key}_missing")
        return False
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        reasons.append(f"{path}.{key}_invalid_type")
        return False
    if not math.isfinite(float(value)):
        reasons.append(f"{path}.{key}_non_finite")
        return False
    return True


def _require_integer(
    container: dict[str, Any],
    key: str,
    path: str,
    reasons: list[str],
    *,
    minimum: int,
) -> bool:
    if key not in container:
        reasons.append(f"{path}.{key}_missing")
        return False
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        reasons.append(f"{path}.{key}_invalid_type")
        return False
    if int(value) < minimum:
        reasons.append(f"{path}.{key}_out_of_range")
        return False
    return True


def _reject_non_finite_values(value: Any, path: str, reasons: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_non_finite_values(child, f"{path}.{key}", reasons)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_non_finite_values(child, f"{path}[{index}]", reasons)
    elif isinstance(value, numbers.Real) and not isinstance(value, bool) and not math.isfinite(float(value)):
        reasons.append(f"{path}_non_finite")


def behavior_health(summary: dict[str, Any], trades: dict[str, Any], baseline_trades: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    action_counts = summary.get("action_counts", {})
    steps = max(1, int(summary.get("steps", 1)))
    hold_ratio = float(action_counts.get("hold", action_counts.get("flat", 0))) / steps
    trade_count = int(trades.get("trades", 0))
    baseline_count = max(1, int(baseline_trades.get("trades", 0)))
    long_count = int(trades.get("long_trades", 0))
    short_count = int(trades.get("short_trades", 0))

    if summary.get("invalid_actions", 0) != 0:
        reasons.append("invalid_actions_nonzero")
    if hold_ratio > 0.99:
        reasons.append("hold_ratio_gt_99pct")
    if trade_count < max(1, int(0.30 * baseline_count)):
        reasons.append("trade_count_lt_30pct_baseline")
    if trade_count == 1:
        reasons.append("single_trade_window")
    if min(long_count, short_count) > 0 and max(long_count, short_count) / min(long_count, short_count) > 10:
        reasons.append("long_short_ratio_gt_10")

    return {
        "decision": "pass" if not reasons else "fail",
        "reasons": reasons,
        "hold_ratio": hold_ratio,
        "trade_count": trade_count,
        "baseline_trade_count": baseline_count,
        "long_short_ratio": None if min(long_count, short_count) == 0 else max(long_count, short_count) / min(long_count, short_count),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _dataclass_to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _clean_date(value: str) -> str:
    return value.replace("-", "").replace(":", "").replace("/", "")


def _git_hash() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _package_versions(names: list[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _data_fingerprint(paths: dict[str, Path]) -> str:
    h = hashlib.sha256()
    for key in sorted(paths):
        path = paths[key]
        h.update(key.encode("utf-8"))
        h.update(str(path).encode("utf-8"))
        try:
            stat = path.stat()
        except OSError:
            h.update(b"missing")
            continue
        h.update(str(stat.st_size).encode("ascii"))
        h.update(str(int(stat.st_mtime_ns)).encode("ascii"))
    return h.hexdigest()
