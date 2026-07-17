import math

import pandas as pd
import pytest

from backend.app.rl.actions import Action
from backend.app.rl.config import RLConfig, RewardConfig
from backend.app.rl.env import TradingEnv
from backend.app.rl.reward import compute_reward
from backend.app.rl.target_env import TargetPosition, TargetPositionEnv


def test_reward_does_not_charge_transaction_cost_twice():
    config = RewardConfig(drawdown_penalty=0.0)
    reward = compute_reward(
        equity_before=1.0,
        equity_after=0.9988,
        peak_equity_before=1.0,
        invalid_action=False,
        flipped=False,
        config=config,
    )
    assert reward == pytest.approx(math.log(0.9988) * config.pnl_scale)


def test_existing_drawdown_is_not_penalized_repeatedly():
    reward = compute_reward(
        equity_before=0.99,
        equity_after=0.99,
        peak_equity_before=1.0,
        invalid_action=False,
        flipped=False,
        config=RewardConfig(),
    )
    assert reward == 0.0


def test_flat_market_round_trip_charges_one_cost_per_side():
    bars = pd.DataFrame({"close": [100.0, 100.0, 100.0]})
    config = RLConfig(fee_rate=0.001, slippage_rate=0.0002, funding_rate_8h=0.0)
    env = TradingEnv(bars, config=config)
    env.reset()
    _, _, _, _, opened = env.step(Action.OPEN_LONG)
    _, _, _, _, closed = env.step(Action.CLOSE)

    expected = (1.0 - 0.0012) ** 2
    assert opened["equity"] == pytest.approx(1.0 - 0.0012)
    assert closed["equity"] == pytest.approx(expected)


def test_new_position_executes_at_next_open_not_current_close():
    bars = pd.DataFrame({
        "open": [100.0, 110.0],
        "close": [100.0, 121.0],
    })
    config = RLConfig(fee_rate=0.0, slippage_rate=0.0, funding_rate_8h=0.0)
    env = TargetPositionEnv(bars, config=config)
    env.reset()
    _, _, terminated, _, info = env.step(TargetPosition.LONG)

    assert terminated
    assert info["execution_price"] == 110.0
    assert info["gap_return"] == 0.0
    assert info["intrabar_return"] == pytest.approx(0.1)
    assert info["equity"] == pytest.approx(1.1)


def test_existing_position_bears_gap_before_next_open_action():
    bars = pd.DataFrame({
        "open": [100.0, 100.0, 90.0],
        "close": [100.0, 100.0, 90.0],
    })
    config = RLConfig(fee_rate=0.0, slippage_rate=0.0, funding_rate_8h=0.0)
    env = TargetPositionEnv(bars, config=config)
    env.reset()
    env.step(TargetPosition.LONG)
    _, _, _, _, info = env.step(TargetPosition.FLAT)

    assert info["gap_return"] == pytest.approx(-0.1)
    assert info["equity"] == pytest.approx(0.9)


def test_terminal_liquidation_charges_exit_cost():
    bars = pd.DataFrame({"open": [100.0, 100.0], "close": [100.0, 100.0]})
    config = RLConfig(fee_rate=0.001, slippage_rate=0.0002, funding_rate_8h=0.0)
    env = TargetPositionEnv(bars, config=config)
    env.reset()
    _, _, terminated, _, info = env.step(TargetPosition.LONG)

    assert terminated
    assert info["terminal_liquidation"]
    assert info["position"] == 0
    assert info["equity"] == pytest.approx((1.0 - 0.0012) ** 2)


def test_market_reward_can_disable_probability_shaping():
    bars = pd.DataFrame({
        "open": [100.0, 100.0, 100.0],
        "close": [100.0, 100.0, 100.0],
    })
    reward = RewardConfig(
        probability_shaping=False,
        trade_action_penalty=0.0,
        unsupported_position_penalty=1.0,
    )
    config = RLConfig(
        fee_rate=0.0,
        slippage_rate=0.0,
        funding_rate_8h=0.0,
        reward=reward,
    )
    env = TargetPositionEnv(bars, config=config)
    env.reset()
    _, step_reward, _, _, _ = env.step(TargetPosition.LONG)
    assert step_reward == 0.0


def test_signed_funding_charges_long_and_credits_short():
    bars = pd.DataFrame({"open": [100.0, 100.0], "close": [100.0, 100.0]})
    config = RLConfig(
        fee_rate=0.0,
        slippage_rate=0.0,
        funding_rate_8h=0.01,
        bar_minutes=480,
    )
    long_env = TargetPositionEnv(bars, config=config)
    long_env.reset()
    _, _, _, _, long_info = long_env.step(TargetPosition.LONG)
    short_env = TargetPositionEnv(bars, config=config)
    short_env.reset()
    _, _, _, _, short_info = short_env.step(TargetPosition.SHORT)

    assert long_info["equity"] == pytest.approx(0.99)
    assert short_info["equity"] == pytest.approx(1.01)
