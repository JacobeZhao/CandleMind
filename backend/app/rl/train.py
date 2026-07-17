"""Training utilities for the RL trading decision layer."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .actions import Action
from .config import RLConfig, RewardConfig
from .data import load_bars_for_feature_set
from .env import TradingEnv
from .evaluate import EvaluationResult, evaluate_policy, threshold_policy
from .experiment import make_model_id, write_train_artifacts
from .feature_engineering import (
    FEATURE_SET_MARKET_V2,
    FEATURE_SET_PROB_V2,
    build_decision_frame,
    build_feature_frame,
)
from .policy import sanitize_action
from .sb3_env import SB3TargetPositionEnv, SB3TradingEnv
from .target_env import TargetPosition, TargetPositionEnv
from .target_evaluate import evaluate_target_policy, threshold_target_policy


@dataclass(frozen=True)
class TrainResult:
    model_path: Path
    run_dir: Path
    manifest_path: Path
    timesteps: int
    pretrain_epochs: int
    mask_actions: bool
    target_position: bool
    gamma: float
    evaluation: EvaluationResult


def gamma_from_half_life(*, bar_minutes: int, half_life_hours: float) -> float:
    if bar_minutes <= 0:
        raise ValueError("bar_minutes must be positive")
    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be positive")
    return math.exp(
        math.log(0.5) * bar_minutes / (60.0 * half_life_hours)
    )


def train_ppo(
    *,
    symbol: str,
    start: str | None,
    end: str | None,
    timesteps: int,
    output_dir: Path,
    seed: int = 42,
    pretrain_epochs: int = 5,
    mask_actions: bool = True,
    target_position: bool = False,
    max_position_bars: int | None = 288,
    position_hold_penalty: float = 0.0,
    directional_exposure_penalty: float = 0.0,
    fee_rate: float = 0.0010,
    slippage_rate: float = 0.0002,
    position_fraction: float = 1.0,
    decision_interval_bars: int = 1,
    funding_rate_8h: float = 0.0,
    discount_half_life_hours: float = 24.0,
    feature_set: str = FEATURE_SET_PROB_V2,
    trend_follow_mode: bool = False,
    min_position_bars: int = 0,
    cooldown_bars: int = 0,
    trend_min_gap: float = 0.06,
    trend_min_confidence: float = 0.52,
    trend_min_hurst: float = 0.50,
    trend_max_vol_regime: float = 2.0,
    trend_monthly_tolerance: float = 0.03,
) -> TrainResult:
    if decision_interval_bars <= 0:
        raise ValueError("decision_interval_bars must be positive")
    if discount_half_life_hours <= 0:
        raise ValueError("discount_half_life_hours must be positive")
    if feature_set == FEATURE_SET_MARKET_V2 and pretrain_epochs > 0:
        raise ValueError("market_v2 does not support probability-threshold pretraining")
    try:
        if mask_actions and not target_position:
            from sb3_contrib import MaskablePPO as PPO
        else:
            from stable_baselines3 import PPO
    except ImportError as exc:
        raise ImportError(
            "stable-baselines3/sb3-contrib are required. Install with: "
            "python -m pip install stable-baselines3 sb3-contrib gymnasium"
        ) from exc

    load_start = start
    if feature_set == FEATURE_SET_MARKET_V2 and start:
        load_start = str((pd.Timestamp(start) - pd.Timedelta(days=35)).date())
    raw_bars = load_bars_for_feature_set(
        symbol, start=load_start, end=end, feature_set=feature_set
    )
    feature_result = build_feature_frame(
        raw_bars,
        feature_set=feature_set,
        output_start=start,
        output_end=end,
    )
    bars = build_decision_frame(feature_result.bars, decision_interval_bars)
    feature_columns = feature_result.feature_columns
    reward_config = RewardConfig(
        position_hold_penalty=position_hold_penalty,
        directional_exposure_penalty=directional_exposure_penalty,
        probability_shaping=feature_set != FEATURE_SET_MARKET_V2,
    )
    config = RLConfig(
        feature_columns=feature_columns,
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
        gym_env = SB3TargetPositionEnv(TargetPositionEnv(bars, config=config))
    else:
        gym_env = SB3TradingEnv(TradingEnv(bars, config=config))

    rollout = min(2048, max(64, min(len(bars) - 1, timesteps)))
    gamma = gamma_from_half_life(
        bar_minutes=config.bar_minutes,
        half_life_hours=discount_half_life_hours,
    )
    model = PPO(
        "MlpPolicy",
        gym_env,
        seed=seed,
        verbose=1,
        n_steps=rollout,
        batch_size=min(64, rollout),
        gamma=gamma,
        learning_rate=3e-4,
    )
    if pretrain_epochs > 0:
        if target_position:
            observations, actions = collect_target_demos(bars, config)
            pretrain_policy(model, observations, actions, action_count=len(TargetPosition), epochs=pretrain_epochs, seed=seed)
        else:
            observations, actions = collect_threshold_demos(bars, config)
            pretrain_policy(model, observations, actions, action_count=len(Action), epochs=pretrain_epochs, seed=seed)

    model.learn(total_timesteps=timesteps)

    algorithm = "target_ppo" if target_position else "maskable_ppo" if mask_actions else "ppo"
    model_id = make_model_id(
        algorithm=algorithm,
        symbol=symbol,
        start=start,
        end=end,
        seed=seed,
        timesteps=timesteps,
    )
    run_dir = output_dir / model_id
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model"
    model.save(model_path)

    if target_position:
        def policy(obs, info):
            action, _ = model.predict(obs, deterministic=True)
            return int(action)
        evaluation = evaluate_target_policy(bars, policy, config=config)
    else:
        def policy(obs, info):
            action, _ = model.predict(obs, deterministic=True)
            return sanitize_action(int(action), int(info.get("position", 0)))
        evaluation = evaluate_policy(bars, policy, config=config)

    from backend.app.datastore import FEATURES_ML_DIR, LABELS_DIR

    artifacts = write_train_artifacts(
        run_dir=run_dir,
        model_path=model_path.with_suffix(".zip"),
        algorithm=algorithm,
        symbol=symbol,
        start=start,
        end=end,
        seed=seed,
        timesteps=timesteps,
        pretrain_epochs=pretrain_epochs,
        mask_actions=mask_actions,
        config=config,
        feature_columns=feature_columns,
        row_count=len(bars),
        evaluation=evaluation,
        data_paths={
            "labels": LABELS_DIR / f"{symbol}_5m_labels.parquet",
            "features": FEATURES_ML_DIR / f"{symbol}_features.parquet",
        },
        feature_set=feature_set,
        feature_scaler=feature_result.scaler,
        training_hyperparameters={
            "gamma": gamma,
            "discount_half_life_hours": discount_half_life_hours,
            "decision_interval_bars": decision_interval_bars,
            "learning_rate": 3e-4,
            "rollout_steps": rollout,
        },
    )
    return TrainResult(
        model_path=model_path.with_suffix(".zip"),
        run_dir=run_dir,
        manifest_path=artifacts["manifest"],
        timesteps=timesteps,
        pretrain_epochs=pretrain_epochs,
        mask_actions=mask_actions,
        target_position=target_position,
        gamma=gamma,
        evaluation=evaluation,
    )


def collect_threshold_demos(bars, config: RLConfig) -> tuple[np.ndarray, np.ndarray]:
    env = TradingEnv(bars, config=config)
    policy = threshold_policy(config.reward.opportunity_threshold, config.reward.opportunity_threshold)
    obs, info = env.reset(seed=0)
    observations = []
    actions = []
    while True:
        action = int(policy(obs, info))
        observations.append(obs.copy())
        actions.append(action)
        obs, _, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            break
    return np.asarray(observations, dtype=np.float32), np.asarray(actions, dtype=np.int64)


def collect_target_demos(bars, config: RLConfig) -> tuple[np.ndarray, np.ndarray]:
    env = TargetPositionEnv(bars, config=config)
    policy = threshold_target_policy(config.reward.opportunity_threshold, config.reward.opportunity_threshold)
    obs, info = env.reset(seed=0)
    observations = []
    actions = []
    while True:
        action = int(policy(obs, info))
        observations.append(obs.copy())
        actions.append(action)
        obs, _, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            break
    return np.asarray(observations, dtype=np.float32), np.asarray(actions, dtype=np.int64)


def pretrain_policy(model, observations: np.ndarray, actions: np.ndarray, *, action_count: int, epochs: int, seed: int) -> None:
    import torch
    import torch.nn.functional as F

    rng = np.random.default_rng(seed)
    device = model.device
    obs_tensor = torch.as_tensor(observations, dtype=torch.float32, device=device)
    action_tensor = torch.as_tensor(actions, dtype=torch.long, device=device)
    counts = np.bincount(actions, minlength=action_count).astype(np.float32)
    weights = counts.sum() / np.maximum(counts, 1.0)
    sample_prob = weights[actions]
    sample_prob = sample_prob / sample_prob.sum()
    print(f"pretrain_action_counts={counts.astype(int).tolist()}", flush=True)
    batch_size = min(1024, len(observations))
    model.policy.set_training_mode(True)
    optimizer = torch.optim.Adam(model.policy.parameters(), lr=3e-3)

    for epoch in range(epochs):
        sampled = rng.choice(len(observations), size=len(observations), replace=True, p=sample_prob)
        losses = []
        for start in range(0, len(sampled), batch_size):
            idx = torch.as_tensor(sampled[start:start + batch_size], dtype=torch.long, device=device)
            features = model.policy.extract_features(obs_tensor[idx])
            latent_pi, _ = model.policy.mlp_extractor(features)
            logits = model.policy.action_net(latent_pi)
            loss = F.cross_entropy(logits, action_tensor[idx])
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.policy.parameters(), 0.5)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        print(f"pretrain_epoch={epoch + 1}/{epochs} loss={np.mean(losses):.4f}", flush=True)
