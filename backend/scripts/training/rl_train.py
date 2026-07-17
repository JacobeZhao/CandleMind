"""Train the RL trading decision layer with PPO.

Example:
    python -m backend.scripts.training.rl_train --symbol BTCUSDT --start 2024-01-01 --end 2024-03-01 --timesteps 20000
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.datastore import MARKET_ROOT
from backend.app.rl.feature_engineering import FEATURE_SET_TREND_FOLLOW_V1
from backend.app.rl.train import train_ppo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--timesteps", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pretrain-epochs", type=int, default=20)
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
    parser.add_argument("--feature-set", default=FEATURE_SET_TREND_FOLLOW_V1, help="RL feature set: v1, prob_v2, or trend_follow_v1")
    parser.add_argument("--trend-follow-mode", action="store_true", help="Constrain target actions to trend-following gates")
    parser.add_argument("--min-position-bars", type=int, default=12, help="Minimum bars to hold while trend remains valid")
    parser.add_argument("--cooldown-bars", type=int, default=12, help="Bars to wait after closing before opening again")
    parser.add_argument("--trend-min-gap", type=float, default=0.06)
    parser.add_argument("--trend-min-confidence", type=float, default=0.52)
    parser.add_argument("--trend-min-hurst", type=float, default=0.50)
    parser.add_argument("--trend-max-vol-regime", type=float, default=2.0)
    parser.add_argument("--trend-monthly-tolerance", type=float, default=0.03)
    parser.add_argument("--output-dir", type=Path, default=MARKET_ROOT / "models" / "rl" / "candidates")
    args = parser.parse_args()

    result = train_ppo(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        timesteps=args.timesteps,
        output_dir=args.output_dir,
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
        trend_min_gap=args.trend_min_gap,
        trend_min_confidence=args.trend_min_confidence,
        trend_min_hurst=args.trend_min_hurst,
        trend_max_vol_regime=args.trend_max_vol_regime,
        trend_monthly_tolerance=args.trend_monthly_tolerance,
    )
    print(result)


if __name__ == "__main__":
    main()
