"""Nested OOS audit for sparse microstructure alpha with a cost hurdle."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as parquet

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.rl.feature_engineering import build_decision_frame
from backend.scripts.evaluation.rl_alpha_baseline import (
    OUTER_WINDOWS,
    forward_open_return,
    hold_comparators,
    moving_block_bootstrap_mean_ci,
    trade_metrics,
)

KEYWORDS = ("taker", "funding", "vwap")
HORIZONS = (24, 72)
TAIL_FRACTIONS = (0.05, 0.10, 0.20)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--position-fraction", type=float, default=0.5)
    parser.add_argument("--fee-rate", type=float, default=0.0010)
    parser.add_argument("--slippage-rate", type=float, default=0.0002)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    decisions, features = load_decisions(args.data_root, args.symbol)
    report = run_cost_hurdle_alpha(
        decisions,
        features=features,
        position_fraction=args.position_fraction,
        one_way_cost=args.fee_rate + args.slippage_rate,
    )
    report.update(
        {
            "symbol": args.symbol,
            "position_fraction": args.position_fraction,
            "fee_rate": args.fee_rate,
            "slippage_rate": args.slippage_rate,
            "features": features,
            "candidate_count": len(features) * len(HORIZONS) * len(TAIL_FRACTIONS),
            "research_status": "exploratory_after_full_sample_feature_audit",
        }
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


def load_decisions(data_root: Path, symbol: str) -> tuple[pd.DataFrame, list[str]]:
    labels_path = data_root / "processed" / "labels" / f"{symbol}_5m_labels.parquet"
    features_path = data_root / "processed" / "features_ml" / f"{symbol}_features.parquet"
    available = parquet.read_schema(features_path).names
    features = [name for name in available if any(key in name.lower() for key in KEYWORDS)]
    labels = pd.read_parquet(
        labels_path,
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    feature_frame = pd.read_parquet(features_path, columns=["open_time", *features])
    bars = labels.merge(feature_frame, on="open_time", how="inner").sort_values("open_time")
    decisions = build_decision_frame(bars.reset_index(drop=True), 12)
    decisions["dt"] = _timestamps(decisions["open_time"])
    for horizon in HORIZONS:
        decisions[f"forward_{horizon}h"] = forward_open_return(decisions, horizon)
    return decisions, features


def run_cost_hurdle_alpha(
    decisions: pd.DataFrame,
    *,
    features: list[str],
    position_fraction: float,
    one_way_cost: float,
) -> dict:
    candidates = [
        {"feature": feature, "horizon_hours": horizon, "tail_fraction": tail}
        for feature, horizon, tail in itertools.product(features, HORIZONS, TAIL_FRACTIONS)
    ]
    folds = []
    base_returns: list[float] = []
    stress_returns: list[float] = []
    entry_months: list[str] = []

    for fold_id, (train_start, train_end, test_start, test_end) in enumerate(OUTER_WINDOWS, start=1):
        rankings = [
            evaluate_candidate_inner(
                decisions,
                candidate,
                train_start=train_start,
                train_end=train_end,
                position_fraction=position_fraction,
                one_way_cost=one_way_cost,
            )
            for candidate in candidates
        ]
        qualified = [item for item in rankings if item["qualified"]]
        qualified.sort(
            key=lambda item: (
                item["inner_mean_trade_return_ci_95"][0],
                item["inner_final_equity"],
            ),
            reverse=True,
        )
        selected = qualified[0] if qualified else None
        base = _flat_result()
        stress = _flat_result()
        outer_rule = None
        outer = pd.DataFrame()
        if selected is not None:
            candidate = selected["candidate"]
            horizon = int(candidate["horizon_hours"])
            train = _eligible_window(decisions, train_start, train_end, horizon_hours=horizon)
            outer = _eligible_window(decisions, test_start, test_end, horizon_hours=horizon)
            outer_rule = fit_tail_rule(train, candidate, one_way_cost=one_way_cost)
            if outer_rule is not None and outer_rule["qualified"]:
                base = simulate_tail_rule(
                    outer,
                    outer_rule,
                    position_fraction=position_fraction,
                    one_way_cost=one_way_cost,
                )
                stress = simulate_tail_rule(
                    outer,
                    outer_rule,
                    position_fraction=position_fraction,
                    one_way_cost=2.0 * one_way_cost,
                )
                base_returns.extend(base["trade_returns"])
                stress_returns.extend(stress["trade_returns"])
                entry_months.extend(base["entry_months"])

        comparator_frame = outer if not outer.empty else _eligible_window(
            decisions, test_start, test_end, horizon_hours=max(HORIZONS)
        )
        folds.append(
            {
                "fold": fold_id,
                "train_window": {"start": train_start, "end": train_end},
                "test_window": {"start": test_start, "end": test_end},
                "qualified_candidates": len(qualified),
                "selected": selected,
                "outer_rule": outer_rule,
                "outer_base": _without_samples(base),
                "outer_2x_cost": _without_samples(stress),
                "comparators": hold_comparators(
                    comparator_frame,
                    position_fraction=position_fraction,
                    one_way_cost=one_way_cost,
                ),
            }
        )

    return {
        "search_space": {
            "horizons": list(HORIZONS),
            "tail_fractions": list(TAIL_FRACTIONS),
            "selection_rule": "highest_positive_2x_cost_inner_ci_or_flat",
            "gross_edge_hurdle": 4.0 * one_way_cost,
        },
        "folds": folds,
        "aggregate": aggregate_results(folds, base_returns, stress_returns, entry_months),
    }


def evaluate_candidate_inner(
    decisions: pd.DataFrame,
    candidate: dict,
    *,
    train_start: str,
    train_end: str,
    position_fraction: float,
    one_way_cost: float,
) -> dict:
    horizon = int(candidate["horizon_hours"])
    equities = []
    returns: list[float] = []
    fitted_rules = 0
    for validation_start, validation_end in _inner_windows(train_end):
        fit = _eligible_window(decisions, train_start, validation_start, horizon_hours=horizon)
        validation = _eligible_window(
            decisions, validation_start, validation_end, horizon_hours=horizon
        )
        rule = fit_tail_rule(fit, candidate, one_way_cost=one_way_cost)
        if rule is None or not rule["qualified"]:
            equities.append(1.0)
            continue
        fitted_rules += 1
        result = simulate_tail_rule(
            validation,
            rule,
            position_fraction=position_fraction,
            one_way_cost=2.0 * one_way_cost,
        )
        equities.append(float(result["final_equity"]))
        returns.extend(result["trade_returns"])

    final_equity = float(np.prod(equities))
    profitable = int(sum(value > 1.0 for value in equities))
    eligible_for_ci = (
        fitted_rules >= 3
        and final_equity > 1.0
        and profitable >= 3
        and len(returns) >= 40
    )
    lower, upper = (0.0, 0.0)
    if eligible_for_ci:
        lower, upper = moving_block_bootstrap_mean_ci(
            np.asarray(returns, dtype=float), replications=1_000, seed=31
        )
    return {
        "candidate": candidate,
        "inner_fold_equities": equities,
        "inner_final_equity": final_equity,
        "inner_profitable_folds": profitable,
        "inner_fitted_rules": fitted_rules,
        "inner_trades": len(returns),
        "inner_mean_trade_return_ci_95": [lower, upper],
        "qualified": bool(
            eligible_for_ci and lower > 0.0
        ),
    }


def fit_tail_rule(frame: pd.DataFrame, candidate: dict, *, one_way_cost: float) -> dict | None:
    feature = str(candidate["feature"])
    horizon = int(candidate["horizon_hours"])
    tail = float(candidate["tail_fraction"])
    target = f"forward_{horizon}h"
    valid = frame[["dt", feature, target]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(valid) < 500 or valid["dt"].dt.to_period("M").nunique() < 3:
        return None
    lower_threshold = float(valid[feature].quantile(tail))
    upper_threshold = float(valid[feature].quantile(1.0 - tail))
    if not np.isfinite(lower_threshold) or lower_threshold >= upper_threshold:
        return None

    ic = float(valid[feature].corr(valid[target], method="spearman"))
    if not np.isfinite(ic) or ic == 0.0:
        return None
    direction = 1 if ic > 0 else -1
    monthly_ics = []
    for _, month in valid.groupby(valid["dt"].dt.to_period("M")):
        if len(month) < 100 or month[feature].nunique() < 2:
            continue
        value = month[feature].corr(month[target], method="spearman")
        if np.isfinite(value):
            monthly_ics.append(float(value))
    sign_consistency = (
        float(np.mean(np.sign(monthly_ics) == direction)) if monthly_ics else 0.0
    )
    provisional = {
        **candidate,
        "direction": direction,
        "lower_threshold": lower_threshold,
        "upper_threshold": upper_threshold,
    }
    gross = simulate_tail_rule(valid, provisional, position_fraction=1.0, one_way_cost=0.0)
    values = np.asarray(gross["trade_returns"], dtype=float)
    if len(values) < 20:
        return None
    edge_lower = float(values.mean() - 1.96 * values.std(ddof=1) / np.sqrt(len(values)))
    edge_hurdle = 4.0 * one_way_cost
    return {
        **provisional,
        "training_rows": len(valid),
        "training_months": int(valid["dt"].dt.to_period("M").nunique()),
        "training_ic": ic,
        "monthly_sign_consistency": sign_consistency,
        "training_trades": len(values),
        "gross_edge_lower_95": edge_lower,
        "gross_edge_hurdle": edge_hurdle,
        "qualified": bool(
            abs(ic) >= 0.02
            and sign_consistency >= 0.60
            and edge_lower > edge_hurdle
        ),
    }


def simulate_tail_rule(
    frame: pd.DataFrame,
    rule: dict,
    *,
    position_fraction: float,
    one_way_cost: float,
) -> dict:
    feature = str(rule["feature"])
    horizon = int(rule["horizon_hours"])
    target = f"forward_{horizon}h"
    direction = int(rule["direction"])
    lower = float(rule["lower_threshold"])
    upper = float(rule["upper_threshold"])
    returns = []
    entry_months = []
    longs = 0
    shorts = 0
    next_entry = 0
    values = frame[feature].to_numpy(dtype=float)
    targets = frame[target].to_numpy(dtype=float)
    times = frame["dt"].to_numpy()
    sides = np.where(values >= upper, direction, np.where(values <= lower, -direction, 0))
    candidate_indices = np.flatnonzero(
        (sides != 0) & np.isfinite(values) & np.isfinite(targets)
    )
    for index in candidate_indices:
        if index < next_entry:
            continue
        side = int(sides[index])
        gross = side * float(targets[index])
        returns.append(position_fraction * gross - 2.0 * position_fraction * one_way_cost)
        entry_months.append(str(pd.Timestamp(times[index]).to_period("M")))
        longs += int(side > 0)
        shorts += int(side < 0)
        next_entry = index + horizon
    result = trade_metrics(returns, long_trades=longs, short_trades=shorts)
    result["entry_months"] = entry_months
    return result


def aggregate_results(
    folds: list[dict],
    base_returns: list[float],
    stress_returns: list[float],
    entry_months: list[str],
) -> dict:
    base_equities = [float(fold["outer_base"]["final_equity"]) for fold in folds]
    stress_equities = [float(fold["outer_2x_cost"]["final_equity"]) for fold in folds]
    active_folds = sum(int(fold["outer_base"]["trades"] > 0) for fold in folds)
    lower, upper = moving_block_bootstrap_mean_ci(
        np.asarray(base_returns, dtype=float), replications=10_000, seed=37
    )
    metrics = trade_metrics(
        base_returns,
        long_trades=sum(int(fold["outer_base"]["long_trades"]) for fold in folds),
        short_trades=sum(int(fold["outer_base"]["short_trades"]) for fold in folds),
    )
    monthly = {}
    for month, value in zip(entry_months, base_returns):
        monthly[month] = monthly.get(month, 1.0) * (1.0 + value)
    profitable_month_rate = (
        float(np.mean([equity > 1.0 for equity in monthly.values()])) if monthly else 0.0
    )
    return {
        "base_final_equity": float(np.prod(base_equities)),
        "base_fold_equities": base_equities,
        "base_profitable_folds": int(sum(value > 1.0 for value in base_equities)),
        "base_mean_trade_return_ci_95": [lower, upper],
        "base_profit_factor": float(metrics["profit_factor"]),
        "base_trades": len(base_returns),
        "active_folds": active_folds,
        "cost_2x_final_equity": float(np.prod(stress_equities)),
        "cost_2x_fold_equities": stress_equities,
        "profitable_months": int(sum(value > 1.0 for value in monthly.values())),
        "active_months": len(monthly),
        "profitable_month_rate": profitable_month_rate,
        "gate_pass": bool(
            np.prod(base_equities) > 1.0
            and sum(value > 1.0 for value in base_equities) >= 3
            and active_folds >= 3
            and len(base_returns) >= 100
            and metrics["profit_factor"] >= 1.10
            and lower > 0.0
            and profitable_month_rate >= 0.60
            and np.prod(stress_equities) > 1.0
        ),
    }


def _inner_windows(train_end: str) -> list[tuple[str, str]]:
    end = pd.Timestamp(train_end)
    start = end - pd.DateOffset(months=8)
    return [
        (
            str((start + pd.DateOffset(months=2 * index)).date()),
            str((start + pd.DateOffset(months=2 * (index + 1))).date()),
        )
        for index in range(4)
    ]


def _eligible_window(
    frame: pd.DataFrame, start: str, end: str, *, horizon_hours: int
) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    mask = (frame["dt"] >= start_ts) & (
        frame["dt"] + pd.Timedelta(hours=horizon_hours, minutes=5) < end_ts
    )
    return frame.loc[mask].reset_index(drop=True)


def _flat_result() -> dict:
    result = trade_metrics([], long_trades=0, short_trades=0)
    result["entry_months"] = []
    return result


def _without_samples(result: dict) -> dict:
    return {
        key: value
        for key, value in result.items()
        if key not in {"trade_returns", "entry_months"}
    }


def _timestamps(series: pd.Series) -> pd.Series:
    if series.dtype.kind == "M":
        return pd.to_datetime(series)
    return pd.to_datetime(series, unit="ms")


if __name__ == "__main__":
    main()
