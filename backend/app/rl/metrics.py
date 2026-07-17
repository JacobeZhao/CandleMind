"""Detailed policy simulation and trading metrics for RL decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from .actions import Action
from .config import RLConfig
from .env import TradingEnv
from .evaluate import EvaluationResult, Policy, evaluate_policy


@dataclass(frozen=True)
class Trade:
    entry_step: int
    exit_step: int
    direction: int
    entry_price: float
    exit_price: float
    return_pct: float
    bars_held: int


@dataclass(frozen=True)
class TradeStats:
    trades: int
    long_trades: int
    short_trades: int
    win_rate: float
    avg_return_pct: float
    median_return_pct: float
    gross_profit_pct: float
    gross_loss_pct: float
    profit_factor: float
    avg_bars_held: float
    best_trade_pct: float
    worst_trade_pct: float


@dataclass(frozen=True)
class DetailedEvaluation:
    summary: EvaluationResult
    trade_stats: TradeStats

    def to_dict(self) -> dict[str, Any]:
        return {"summary": asdict(self.summary), "trade_stats": asdict(self.trade_stats)}


def evaluate_policy_detailed(
    bars: pd.DataFrame,
    policy: Policy,
    config: RLConfig | None = None,
) -> DetailedEvaluation:
    env = TradingEnv(bars, config=config)
    obs, info = env.reset(seed=0)
    total_reward = 0.0
    invalid_actions = 0
    max_drawdown = 0.0
    action_counts = {a.name.lower(): 0 for a in Action}
    steps = 0

    open_trade: dict[str, Any] | None = None
    trades: list[Trade] = []

    while True:
        position_before = int(info.get("position", 0))
        step_before = int(info.get("step_index", 0))
        price = float(env.bars.iloc[step_before]["close"])
        action = int(policy(obs, info))
        action_counts[Action(action).name.lower()] += 1

        obs, reward, terminated, truncated, info = env.step(action)
        position_after = int(info.get("position", 0))
        executed = not bool(info.get("invalid_action"))

        if executed:
            if position_before == 0 and position_after != 0:
                open_trade = {
                    "entry_step": step_before,
                    "direction": position_after,
                    "entry_price": price,
                }
            elif position_before != 0 and position_after == 0 and open_trade is not None:
                trades.append(_close_trade(open_trade, step_before, price))
                open_trade = None
            elif position_before != 0 and position_after != 0 and position_before != position_after:
                if open_trade is not None:
                    trades.append(_close_trade(open_trade, step_before, price))
                open_trade = {
                    "entry_step": step_before,
                    "direction": position_after,
                    "entry_price": price,
                }

        total_reward += reward
        invalid_actions += int(bool(info.get("invalid_action")))
        peak = float(info["peak_equity"])
        equity = float(info["equity"])
        drawdown = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        max_drawdown = max(max_drawdown, drawdown)
        steps += 1
        if terminated or truncated:
            if open_trade is not None:
                last_step = int(info.get("step_index", step_before))
                last_price = float(env.bars.iloc[last_step]["close"])
                trades.append(_close_trade(open_trade, last_step, last_price))
            break

    summary = EvaluationResult(
        steps=steps,
        final_equity=float(info["equity"]),
        total_reward=float(total_reward),
        max_drawdown=float(max_drawdown),
        invalid_actions=invalid_actions,
        action_counts=action_counts,
    )
    return DetailedEvaluation(summary=summary, trade_stats=compute_trade_stats(trades))


def compute_trade_stats(trades: list[Trade]) -> TradeStats:
    if not trades:
        return TradeStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    returns = np.asarray([t.return_pct for t in trades], dtype=float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(losses.sum()) if len(losses) else 0.0
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else float("inf") if gross_profit > 0 else 0.0
    finite_pf = profit_factor if np.isfinite(profit_factor) else 999.0
    return TradeStats(
        trades=len(trades),
        long_trades=sum(1 for t in trades if t.direction == 1),
        short_trades=sum(1 for t in trades if t.direction == -1),
        win_rate=float((returns > 0).mean()),
        avg_return_pct=float(returns.mean()),
        median_return_pct=float(np.median(returns)),
        gross_profit_pct=gross_profit,
        gross_loss_pct=gross_loss,
        profit_factor=float(finite_pf),
        avg_bars_held=float(np.mean([t.bars_held for t in trades])),
        best_trade_pct=float(returns.max()),
        worst_trade_pct=float(returns.min()),
    )


def _close_trade(open_trade: dict[str, Any], exit_step: int, exit_price: float) -> Trade:
    direction = int(open_trade["direction"])
    entry_step = int(open_trade["entry_step"])
    entry_price = float(open_trade["entry_price"])
    if entry_price <= 0:
        ret = 0.0
    else:
        ret = ((exit_price / entry_price) - 1.0) * direction
    return Trade(
        entry_step=entry_step,
        exit_step=exit_step,
        direction=direction,
        entry_price=entry_price,
        exit_price=float(exit_price),
        return_pct=float(ret),
        bars_held=max(0, exit_step - entry_step),
    )
