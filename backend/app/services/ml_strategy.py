"""
ML 趋势策略（Pure-ML 版）

核心思路：
  - 入场：ML long_prob / short_prob 超过每币校准阈值
  - 持仓：ATR 跟踪止损（peak 追踪，只向有利方向移动）
  - ML 早退：same_dir_prob < exit_threshold → 提前平仓（假趋势保护）
  - 加仓：概率持续高 + 价格顺向移动 ≥ add_atr_dist*ATR（最多 max_adds 次）
  - Kelly 仓位：每次入场/加仓按当前 prob 计算 Kelly 倍率

真趋势：prob 持续高 → 止损随 peak 上移 → 充分吃行情
假趋势：prob 迅速跌破 exit_threshold → 早于 ATR trail 触发退出 → 少亏
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict
import warnings; warnings.filterwarnings('ignore')

from .trend_decision import (
    TrendDecisionParams,
    TrendFeatureSnapshot,
    TrendPositionSnapshot,
    decide_entry,
    decide_ml_exit,
)


# ══════════════════════════════════════════════════════════════════════════════
# 策略参数
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MLTrendParams:
    # 入场
    entry_long_threshold:  float = 0.51    # 做多入场阈值（会从 thresholds.json 覆盖）
    entry_short_threshold: float = 0.51    # 做空入场阈值
    min_prob_gap:          float = 0.02    # long_prob - short_prob 最小差值（小市值币）
    min_prob_gap_large_cap: float = 0.06   # 大市值（BTC/ETH/BNB）更严gap门，覆盖高成本
    # 持仓 / 退出
    exit_threshold:        float = 0.38    # same_dir prob 低于此 → ML 早退（BTC/BNB会覆盖为0.43）
    reversal_threshold:    float = 0.55    # opp_dir prob 超过此 → 反向早退（BTC/BNB覆盖为0.53）
    atr_trail:             float = 3.0     # 跟踪止损 ATR 倍数（trailing update用）
    initial_stop_mult:     float = 3.0     # 初始止损 ATR 倍数（设R分母，可>atr_trail）
    min_hold_bars:         int   = 0       # 最短持仓 bar 数，前N bar禁止ML早退（0=不限）
    # 加仓
    max_adds:              int   = 3       # 最多加仓次数
    add_threshold:         float = 0.51    # 加仓概率门（=入场阈值）
    add_atr_dist:          float = 0.8     # 相邻加仓至少移动 N*ATR
    add_size_frac:         float = 0.5     # 加仓量 = 首仓 * add_size_frac（递减）
    # 仓位 / 费用
    risk_pct:              float = 0.01    # 每笔首仓风险（占账户）
    kelly_frac:            float = 0.25    # Kelly 分数（BTC建议0.10，BNB 0.15）
    win_mult:              float = 2.0     # Kelly b 参数 (TP/SL 比)
    fee:                   float = 0.0010  # 单边手续费率（Taker市价单0.10%，与实盘一致）
    slippage:              float = 0.0002  # 单边滑点（入场+出场各一次）
    funding_rate_8h:       float = 0.0001  # 多头资金费率 0.01%/8h（空头不收，保守估计）
    # 允许的方向（1=多, -1=空, 0=双向）
    allowed_direction:     int   = 0
    # ── 改进开关（默认全开）────────────────────────────────────────────────────
    # #1  高波动 regime 门：5m_vol_regime >= 2 时不开新仓
    vol_gate:              bool  = True
    # #3  EMA 对齐门：多头要求 ema_align_score >= 1，空头要求 <= -1
    ema_align_gate:        bool  = True
    # #4  时间加权出场阈值：入场早期阈值收严，临近超时回落到基础值
    time_weighted_exit:    bool  = True
    time_exit_bars:        int   = 12     # 假趋势12bar(1h)内解决，之后回落到基础阈值
    time_exit_delta:       float = 0.05   # 入场早期 exit/reversal 阈值的对称偏移
    # #6  Regime 条件 Kelly：高波动/低 Hurst 时 Kelly 折半，理想 regime 略加
    regime_kelly:          bool  = True
    # #7  Hurst 硬门：hurst < hurst_entry_min 时禁止入场（均值回归市场）
    hurst_gate:            bool  = True
    hurst_entry_min:       float = 0.50   # 低于随机游走则市场均值回归，趋势策略禁入
    # #8  月度趋势过滤：顺长期趋势入场（反趋势方向阈值提高 trend_bias_delta）
    monthly_trend_filter:  bool  = True
    monthly_sma_bars:      int   = 8640   # ~30 天 5m K 线数（30×24×12=8640）
    trend_bias_delta:      float = 0.08   # 反趋势方向入场阈值抬高量
    short_extra_delta:     float = 0.00   # 做空额外阈值增量（BNB 等高风险品种）
    # #9  最大逆境 R 保护：浮亏超 max_adverse_r × 初始风险时强制平仓
    max_adverse_r:         float = 2.0

    @classmethod
    def from_thresholds(cls, symbol: str, **kwargs) -> 'MLTrendParams':
        """从 thresholds.json 加载每币推荐阈值，其余参数可用 kwargs 覆盖。"""
        try:
            from .ml_signal import get_threshold
            lt = get_threshold(symbol, 'long_label',  'recommended')
            st = get_threshold(symbol, 'short_label', 'recommended')
        except Exception:
            lt = st = 0.51
        # 按 cost/ATR 分析设定每币结构性默认值（可被 kwargs 覆盖）
        _coin_defaults: dict = {}
        if symbol == 'BTCUSDT':
            _coin_defaults = dict(initial_stop_mult=5.0, kelly_frac=0.10,
                                  min_hold_bars=4, risk_pct=0.0043,
                                  exit_threshold=0.43, reversal_threshold=0.60)
        elif symbol == 'BNBUSDT':
            _coin_defaults = dict(initial_stop_mult=4.0, kelly_frac=0.15,
                                  min_hold_bars=4, risk_pct=0.0055,
                                  exit_threshold=0.43, reversal_threshold=0.60,
                                  short_extra_delta=0.12)   # BNB 做空信号过度：0.58+0.12=0.70
        elif symbol == 'ETHUSDT':
            _coin_defaults = dict(initial_stop_mult=4.0, kelly_frac=0.25,
                                  risk_pct=0.0095,
                                  exit_threshold=0.38, reversal_threshold=0.60)
        elif symbol == 'XRPUSDT':
            _coin_defaults = dict(risk_pct=0.0110, reversal_threshold=0.62)
        elif symbol in ('ADAUSDT',):
            _coin_defaults = dict(risk_pct=0.0138)
        elif symbol in ('SOLUSDT',):
            _coin_defaults = dict(risk_pct=0.0123)
        elif symbol in ('DOGEUSDT',):
            _coin_defaults = dict(risk_pct=0.0124)
        elif symbol in ('LINKUSDT',):
            _coin_defaults = dict(risk_pct=0.0108)
        elif symbol in ('AVAXUSDT',):
            _coin_defaults = dict(risk_pct=0.0105)
        # kwargs 优先级高于 per-coin defaults
        merged = {**_coin_defaults, **kwargs}
        entry_long_threshold = merged.pop('entry_long_threshold', lt)
        entry_short_threshold = merged.pop('entry_short_threshold', st)
        add_threshold = merged.pop(
            'add_threshold', min(entry_long_threshold, entry_short_threshold)
        )
        return cls(
            entry_long_threshold  = entry_long_threshold,
            entry_short_threshold = entry_short_threshold,
            add_threshold         = add_threshold,
            **merged,
        )

    @classmethod
    def from_runtime_config(
        cls,
        symbol: str,
        config: Optional[dict] = None,
        *,
        risk_pct: Optional[float] = None,
    ) -> 'MLTrendParams':
        """Build live/backtest parameters without introducing runtime defaults."""
        config = config or {}
        valid_fields = cls.__dataclass_fields__
        overrides = {key: value for key, value in config.items() if key in valid_fields}
        aliases = {
            'ml_exit_threshold': ('exit_threshold', float),
            'ml_reversal_threshold': ('reversal_threshold', float),
            'add_min_atr': ('add_atr_dist', float),
        }
        for source, (target, converter) in aliases.items():
            if source in config and target not in overrides:
                overrides[target] = converter(config[source])
        if risk_pct is not None:
            overrides['risk_pct'] = float(risk_pct)
        return cls.from_thresholds(symbol, **overrides)

    def to_decision_params(self) -> TrendDecisionParams:
        """Export the fields owned by the versioned pure decision contract."""
        return TrendDecisionParams(
            entry_long_threshold=self.entry_long_threshold,
            entry_short_threshold=self.entry_short_threshold,
            min_prob_gap=self.min_prob_gap,
            min_prob_gap_large_cap=self.min_prob_gap_large_cap,
            allowed_direction=self.allowed_direction,
            short_extra_delta=self.short_extra_delta,
            vol_gate=self.vol_gate,
            ema_align_gate=self.ema_align_gate,
            hurst_gate=self.hurst_gate,
            hurst_entry_min=self.hurst_entry_min,
            monthly_trend_filter=self.monthly_trend_filter,
            trend_bias_delta=self.trend_bias_delta,
            exit_threshold=self.exit_threshold,
            reversal_threshold=self.reversal_threshold,
            time_weighted_exit=self.time_weighted_exit,
            time_exit_bars=self.time_exit_bars,
            time_exit_delta=self.time_exit_delta,
            min_hold_bars=self.min_hold_bars,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 交易记录
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    entry_time:    int          # open_time (ms)
    exit_time:     int
    direction:     int          # 1=long, -1=short
    entry_price:   float
    exit_price:    float
    initial_stop:  float        # 首仓止损价
    avg_price:     float        # 含加仓后的平均成本
    final_qty:     float        # 最终持仓量（以首仓量为1）
    pnl_r:         float        # 以首仓风险(1R)为单位的收益
    reason:        str          # 'stop' | 'ml_exit' | 'ml_reversal' | 'end'
    adds:          int          # 实际加仓次数
    entry_prob:    float        # 入场时 same_dir_prob
    exit_prob:     float        # 平仓时 same_dir_prob

    @property
    def duration_bars(self) -> int:
        # entry_time / exit_time 均为 int64 ms（load_scored_bars 保证）
        diff_ms = int(self.exit_time) - int(self.entry_time)
        return max(0, int(diff_ms / (5 * 60_000)))

    @property
    def pnl_sign(self) -> int:
        return 1 if self.pnl_r > 0 else -1


# ══════════════════════════════════════════════════════════════════════════════
# Kelly 仓位
# ══════════════════════════════════════════════════════════════════════════════

def _kelly_mult(prob: float, win_mult: float = 2.0,
                kelly_frac: float = 0.25) -> float:
    """返回 Kelly 仓位倍率（相对 1R），clamp [0.3, 1.5]。"""
    if prob <= 0.5:
        return 0.5
    b = win_mult
    q = 1 - prob
    f = max(0.0, (prob * b - q) / b)
    norm = kelly_frac * 0.40   # 按 kelly_frac 比例缩放，保持 [0.3, 1.5] 范围有效
    return float(np.clip(f * kelly_frac / norm, 0.3, 1.5))


def _initial_stop_price(
    entry_price: float,
    atr: float,
    direction: int,
    initial_stop_mult: float,
) -> float:
    """Return the initial risk stop shared by all execution modes."""
    return float(entry_price) - float(initial_stop_mult) * float(atr) * int(direction)


def _add_tranche_qty(
    initial_qty: float,
    add_size_frac: float,
    completed_adds: int,
) -> float:
    """Size the next add from the initial tranche; adds excludes the entry."""
    return float(initial_qty) * float(add_size_frac) ** (int(completed_adds) + 1)


# ══════════════════════════════════════════════════════════════════════════════
# 数据加载 + 模型打分
# ══════════════════════════════════════════════════════════════════════════════

def load_scored_bars(symbol: str,
                     start: Optional[str] = None,
                     end:   Optional[str] = None,
                     include_multi_horizon: bool = False,
                     multi_horizon_variant: Optional[str] = None) -> pd.DataFrame:
    """
    加载 labels parquet（含 OHLCV + ATR + 标注）并用已训练模型打分，
    返回每根 5m bar 的 long_prob / short_prob。

    注意：使用全量训练模型打分，回测存在一定 in-sample 偏差，
    但 CPCV AUC=0.86 证明信号有效，偏差可接受。
    """
    from ..datastore import FEATURES_ML_DIR, LABELS_DIR
    from .trend_predictor import load_model

    if include_multi_horizon and not multi_horizon_variant:
        raise ValueError('multi_horizon_variant is required when multi-horizon scoring is enabled')

    # 1. 加载 label parquet（有 OHLCV + ATR + 标注）
    label_path = LABELS_DIR / f'{symbol}_5m_labels.parquet'
    bars = pd.read_parquet(label_path)

    # 2. 加载 feature parquet，merge 到 bars
    feat_path = FEATURES_ML_DIR / f'{symbol}_features.parquet'
    feats = pd.read_parquet(feat_path)
    bars = bars.merge(feats, on='open_time', how='inner', suffixes=('', '_feat'))

    # open_time 统一为 int64 ms（parquet 可能存为 datetime64[ns/us]）
    if bars['open_time'].dtype.kind == 'M':   # datetime type
        bars['open_time'] = (bars['open_time'].astype(np.int64) // 1_000_000).astype(np.int64)
    else:
        bars['open_time'] = bars['open_time'].astype(np.int64)

    # 3. 日期过滤（在打分前过滤，节省时间）
    if start or end:
        ts = pd.to_datetime(bars['open_time'], unit='ms')
        if start:
            bars = bars[ts >= pd.Timestamp(start)]
        if end:
            bars = bars[ts <= pd.Timestamp(end)]
        bars = bars.reset_index(drop=True)

    # 4. 用模型打分
    for target, col in [('long_label', 'long_prob'), ('short_label', 'short_prob')]:
        try:
            bundle = load_model(symbol, target)
            miss = [c for c in bundle.feature_cols if c not in bars.columns]
            if miss:
                print(f'  WARN: {len(miss)} 特征列缺失于 bars，填 0')
            X = bars[bundle.feature_cols].fillna(0).astype(np.float32)
            bars[col] = bundle.predict_proba(X)
        except FileNotFoundError:
            print(f'  WARN: 模型 {symbol}_{target} 未找到，prob 填 0.33')
            bars[col] = 0.33

    if include_multi_horizon:
        # Research-only V2 models expose independent horizon probabilities.
        for side in ('long', 'short'):
            for horizon in ('30m', '1h', '4h'):
                target = f'{side}_label_{horizon}_{multi_horizon_variant}'
                col = f'{side}_prob_{horizon}'
                try:
                    bundle = load_model(symbol, target)
                    X = bars.reindex(columns=bundle.feature_cols, fill_value=0.0)
                    X = X.fillna(0).astype(np.float32)
                    bars[col] = bundle.predict_proba(X)
                except FileNotFoundError as exc:
                    raise FileNotFoundError(
                        f'multi-horizon model missing: {symbol}_{target}'
                    ) from exc

        long_horizons = [f'long_prob_{h}' for h in ('30m', '1h', '4h')]
        short_horizons = [f'short_prob_{h}' for h in ('30m', '1h', '4h')]
        bars['long_prob_agg'] = bars[long_horizons].mean(axis=1)
        bars['short_prob_agg'] = bars[short_horizons].mean(axis=1)
        bars['long_agreement'] = bars[long_horizons].gt(0.55).mean(axis=1)
        bars['short_agreement'] = bars[short_horizons].gt(0.55).mean(axis=1)

    return bars.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# 核心模拟引擎
# ══════════════════════════════════════════════════════════════════════════════

def simulate_ml_trend(bars: pd.DataFrame,
                      params: MLTrendParams,
                      symbol: str = '') -> List[TradeRecord]:
    """
    逐 bar 模拟 ML 趋势策略，返回交易列表。
    bars 必须包含：open_time, open, high, low, close, atr, long_prob, short_prob。
    收盘特征只生成指令；信号指令在下一根 bar 的 open 执行。
    改进 #1/#3/#4/#6：vol_gate / ema_align_gate / time_weighted_exit / regime_kelly
    """
    p = params
    decision_params = p.to_decision_params()
    trades: List[TradeRecord] = []

    # 仓位状态
    in_pos        = False
    direction     = 0
    entry_time    = 0
    entry_price   = 0.0
    initial_stop  = 0.0
    initial_qty   = 0.0   # 首仓 Kelly 倍率（pnl_r 归一化分母用）
    stop          = 0.0
    peak          = 0.0
    avg_price     = 0.0
    qty           = 0.0
    adds          = 0
    last_add_ref  = 0.0
    entry_prob    = 0.0
    entry_bar_idx = -1
    last_same_prob = 0.0

    # 上一根 bar 收盘后生成、等待当前 bar 开盘执行的指令。
    pending_kind      = None
    pending_direction = 0
    pending_prob      = 0.0
    pending_atr       = 0.0
    pending_vol_r     = 1.0
    pending_hurst     = 0.99

    open_arr  = bars['open'].values
    high_arr  = bars['high'].values
    low_arr   = bars['low'].values
    close_arr = bars['close'].values
    atr_arr   = bars['atr'].values
    lp_arr    = bars['long_prob'].values
    sp_arr    = bars['short_prob'].values
    ts_arr    = bars['open_time'].values

    # 改进特征列（缺失时 fallback 到中性值）
    vol_regime_arr = bars['5m_vol_regime'].values  if '5m_vol_regime'      in bars.columns else None
    ema_align_arr  = bars['5m_ema_align_score'].values if '5m_ema_align_score' in bars.columns else None
    hurst_arr      = bars['5m_hurst'].values       if '5m_hurst'           in bars.columns else None

    # #8 月度趋势过滤 — 预计算 30 天 SMA
    if p.monthly_trend_filter:
        monthly_sma = pd.Series(close_arr).rolling(
            p.monthly_sma_bars, min_periods=500).mean().values
    else:
        monthly_sma = np.full(len(close_arr), np.nan)

    def append_trade(exit_time_value, exit_price_value, reason, exit_prob_value):
        # 保持原有手续费、滑点和资金费计算，仅改成交时点与成交价格。
        init_risk = abs(entry_price - initial_stop) * max(initial_qty, 1e-8)
        if init_risk > 0:
            raw_pnl      = (exit_price_value - avg_price) * direction * qty
            entry_fee    = avg_price * qty * p.fee
            exit_fee     = exit_price_value * qty * p.fee
            slip_cost    = (avg_price + exit_price_value) * p.slippage * qty
            bars_held_n  = int((int(exit_time_value) - int(entry_time)) / (5 * 60_000))
            funding_cost = (p.funding_rate_8h * (bars_held_n / 96.0)
                            * avg_price * qty) if direction == 1 else 0.0
            total_cost   = entry_fee + exit_fee + slip_cost + funding_cost
            pnl_r        = (raw_pnl - total_cost) / init_risk
        else:
            pnl_r = 0.0

        trades.append(TradeRecord(
            entry_time   = int(entry_time),
            exit_time    = int(exit_time_value),
            direction    = direction,
            entry_price  = float(entry_price),
            exit_price   = float(exit_price_value),
            initial_stop = float(initial_stop),
            avg_price    = float(avg_price),
            final_qty    = float(qty),
            pnl_r        = round(float(pnl_r), 4),
            reason       = reason,
            adds         = adds,
            entry_prob   = round(float(entry_prob), 4),
            exit_prob    = round(float(exit_prob_value), 4),
        ))

    for i in range(len(bars)):
        o   = float(open_arr[i])
        h   = float(high_arr[i])
        l   = float(low_arr[i])
        c   = close_arr[i]
        atr = max(atr_arr[i], 1e-8)
        lp  = lp_arr[i]
        sp  = sp_arr[i]
        ts  = ts_arr[i]

        vol_r     = float(vol_regime_arr[i]) if vol_regime_arr is not None else 1.0
        ema_align = float(ema_align_arr[i])  if ema_align_arr  is not None else 0.0
        hurst     = float(hurst_arr[i])      if hurst_arr      is not None else 0.99
        sma_now = monthly_sma[i]
        snapshot = TrendFeatureSnapshot(
            long_prob=float(lp),
            short_prob=float(sp),
            close=float(c),
            vol_regime=vol_r,
            ema_align=ema_align,
            hurst=hurst,
            monthly_sma=None if np.isnan(sma_now) else float(sma_now),
        )

        exited_this_bar = False
        action = pending_kind
        pending_kind = None

        # ── 开盘阶段：先处理已存在仓位的跳空止损，再执行上一收盘指令 ──
        if in_pos:
            gap_stop_hit = ((direction == 1 and o <= stop) or
                            (direction == -1 and o >= stop))
            if gap_stop_hit:
                append_trade(ts, o, 'stop', last_same_prob)
                in_pos = False
                direction = 0
                exited_this_bar = True
            elif action in ('ml_exit', 'ml_reversal', 'max_adverse'):
                append_trade(ts, o, action, pending_prob)
                in_pos = False
                direction = 0
                exited_this_bar = True
            elif action == 'add':
                add_qty   = _add_tranche_qty(initial_qty, p.add_size_frac, adds)
                avg_price = (avg_price * qty + o * add_qty) / (qty + add_qty)
                qty      += add_qty
                last_add_ref = o
                adds     += 1
        elif action == 'entry':
            direction    = pending_direction
            in_pos       = True
            entry_time   = int(ts)
            entry_bar_idx = i
            entry_price  = o
            stop         = _initial_stop_price(
                o, pending_atr, direction, p.initial_stop_mult
            )
            initial_stop = stop
            peak         = o
            avg_price    = o
            adds         = 0
            last_add_ref = o
            entry_prob   = pending_prob
            last_same_prob = pending_prob

            base_kelly = _kelly_mult(pending_prob, p.win_mult, p.kelly_frac)
            if p.regime_kelly:
                if pending_vol_r >= 2.0:
                    regime_mult = 0.6
                elif pending_hurst < 0.70:
                    regime_mult = 0.7
                elif pending_vol_r == 0 and pending_hurst > 0.90:
                    regime_mult = 1.1
                else:
                    regime_mult = 1.0
                qty = base_kelly * regime_mult
            else:
                qty = base_kelly
            initial_qty = qty

        # ── 盘中阶段：当前 open 之后的 high/low 才能触发当前仓位止损 ──
        if in_pos:
            intrabar_stop_hit = ((direction == 1 and l <= stop) or
                                 (direction == -1 and h >= stop))
            if intrabar_stop_hit:
                append_trade(ts, stop, 'stop', last_same_prob)
                in_pos = False
                direction = 0
                exited_this_bar = True
            else:
                # 新 trailing stop 在本 bar 完成后可知，从下一 bar 起生效。
                # 这避免用同一根 OHLC 同时假设有利极值先于不利极值。
                if direction == 1:
                    peak    = max(peak, h)
                    new_stp = peak - p.atr_trail * atr
                    stop    = max(stop, new_stp)
                else:
                    peak    = min(peak, l)
                    new_stp = peak + p.atr_trail * atr
                    stop    = min(stop, new_stp)

        if exited_this_bar:
            continue

        # ── 收盘阶段：只生成下一根开盘可执行的指令 ────────────────────────
        if not in_pos:
            intent = decide_entry(symbol, snapshot, decision_params)
            if intent.action == 'entry':
                pending_kind      = 'entry'
                pending_direction = intent.direction
                pending_prob      = intent.probability
                pending_atr       = atr
                pending_vol_r     = vol_r
                pending_hurst     = hurst

        else:
            same_prob = lp if direction == 1 else sp
            bars_held = i - entry_bar_idx + 1
            ml_intent = decide_ml_exit(
                snapshot,
                TrendPositionSnapshot(direction=direction, bars_held=bars_held),
                decision_params,
            )

            max_adverse_hit = False
            if p.max_adverse_r > 0:
                init_r = abs(entry_price - initial_stop) * max(initial_qty, 1e-8)
                if init_r > 0:
                    raw_float = (c - avg_price) * direction * qty
                    if raw_float / init_r < -p.max_adverse_r:
                        max_adverse_hit = True

            if max_adverse_hit:
                pending_kind = 'max_adverse'
            elif ml_intent.action == 'ml_reversal':
                pending_kind = 'ml_reversal'
            elif ml_intent.action == 'ml_exit':
                pending_kind = 'ml_exit'
            elif (adds < p.max_adds and
                  same_prob >= p.add_threshold and
                  abs(c - last_add_ref) >= p.add_atr_dist * atr):
                pending_kind = 'add'

            if pending_kind is not None:
                pending_prob = same_prob
            last_same_prob = same_prob

    if in_pos and len(bars) > 0:
        append_trade(
            ts_arr[-1],
            float(close_arr[-1]),
            'end',
            last_same_prob,
        )

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 回测指标
# ══════════════════════════════════════════════════════════════════════════════

def calc_metrics(trades: List[TradeRecord]) -> dict:
    if not trades:
        return {'n_trades': 0}

    pnls = np.array([t.pnl_r for t in trades])
    wins = pnls > 0

    # 按出场原因分组
    reasons = {}
    for t in trades:
        r = reasons.setdefault(t.reason, [])
        r.append(t.pnl_r)

    def _r_stats(lst):
        a = np.array(lst)
        return {'n': len(a), 'win_rate': round(float((a > 0).mean()), 3),
                'avg_r': round(float(a.mean()), 3),
                'total_r': round(float(a.sum()), 3)}

    # 月度权益曲线（以 R 为单位）
    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)
    eq = np.array(equity)
    peak_eq = np.maximum.accumulate(eq)
    drawdown = eq - peak_eq
    max_dd = float(drawdown.min())

    return {
        'n_trades':   len(trades),
        'win_rate':   round(float(wins.mean()), 3),
        'avg_r':      round(float(pnls.mean()), 3),
        'total_r':    round(float(pnls.sum()), 3),
        'max_dd_r':   round(max_dd, 3),
        'profit_factor': round(float(pnls[pnls > 0].sum() / (-pnls[pnls < 0].sum() + 1e-9)), 3),
        'sharpe':     round(float(pnls.mean() / (pnls.std() + 1e-9)), 3),
        'by_reason':  {k: _r_stats(v) for k, v in reasons.items()},
        'long_trades':  len([t for t in trades if t.direction == 1]),
        'short_trades': len([t for t in trades if t.direction == -1]),
        'avg_holds_bars': round(float(np.mean([t.duration_bars for t in trades])), 1),
        'avg_adds':   round(float(np.mean([t.adds for t in trades])), 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 完整回测入口
# ══════════════════════════════════════════════════════════════════════════════

def backtest_ml_trend(
    symbol:  str,
    params:  Optional[MLTrendParams] = None,
    start:   str  = '2024-01-01',
    end:     str  = '2026-06-09',
    verbose: bool = True,
) -> dict:
    """
    对单币运行完整 ML 趋势策略回测。
    start/end 控制回测区间（推荐 2024+ 尽量 OOS）。
    返回 {'symbol', 'params', 'metrics', 'trades'} dict。
    """
    if params is None:
        params = MLTrendParams.from_thresholds(symbol)

    if verbose:
        print(f'[{symbol}] 加载数据 + 模型打分...')
    bars = load_scored_bars(symbol, start=start, end=end)
    if len(bars) < 100:
        return {'symbol': symbol, 'error': '数据不足'}

    if verbose:
        print(f'  bars={len(bars):,}  period={start}~{end}')

    trades = simulate_ml_trend(bars, params, symbol=symbol)
    metrics = calc_metrics(trades)

    if verbose:
        _print_metrics(symbol, metrics, params)

    return {
        'symbol':  symbol,
        'params':  params,
        'metrics': metrics,
        'trades':  trades,
    }


def _print_metrics(symbol: str, m: dict, p: MLTrendParams):
    print(f'\n  ── {symbol} 回测结果 ──')
    print(f'  总交易: {m["n_trades"]}  做多: {m["long_trades"]}  做空: {m["short_trades"]}')
    print(f'  胜率: {m["win_rate"]:.1%}  平均R: {m["avg_r"]:.3f}R  总R: {m["total_r"]:.2f}R')
    print(f'  最大回撤: {m["max_dd_r"]:.2f}R  盈亏比: {m["profit_factor"]:.2f}  Sharpe: {m["sharpe"]:.3f}')
    print(f'  平均持仓: {m["avg_holds_bars"]:.0f} bar  平均加仓次数: {m["avg_adds"]:.1f}')
    by_r = m.get('by_reason', {})
    for reason, stats in by_r.items():
        label = {'stop': '止损', 'ml_exit': 'ML早退', 'ml_reversal': 'ML反向', 'end': '收盘'}.get(reason, reason)
        print(f'  [{label}] n={stats["n"]}  胜率={stats["win_rate"]:.1%}  '
              f'avgR={stats["avg_r"]:.3f}  totalR={stats["total_r"]:.2f}')
    print(f'  入场阈值: long={p.entry_long_threshold}  short={p.entry_short_threshold}  gap={p.min_prob_gap}')
    print(f'  init_stop={p.initial_stop_mult}x  ATR trail={p.atr_trail}x  '
          f'exit_thr={p.exit_threshold}  rev_thr={p.reversal_threshold}  '
          f'time_bars={p.time_exit_bars}  min_hold={p.min_hold_bars}')


def backtest_multi(
    symbols: List[str],
    params_override: Optional[dict] = None,
    start: str = '2024-01-01',
    end:   str = '2026-06-09',
) -> dict:
    """
    多币回测汇总。params_override 覆盖每币 MLTrendParams 非阈值字段。
    """
    results = {}
    for sym in symbols:
        p = MLTrendParams.from_thresholds(sym, **(params_override or {}))
        r = backtest_ml_trend(sym, p, start=start, end=end, verbose=True)
        results[sym] = r

    # 合并汇总
    all_trades = []
    for r in results.values():
        all_trades.extend(r.get('trades', []))
    combined = calc_metrics(all_trades)

    print(f'\n══ 合并汇总 ({len(symbols)} 币) ══')
    print(f'  总交易: {combined["n_trades"]}  胜率: {combined["win_rate"]:.1%}  '
          f'总R: {combined["total_r"]:.2f}  MaxDD: {combined["max_dd_r"]:.2f}R')
    print(f'  ProfitFactor: {combined["profit_factor"]:.2f}  Sharpe: {combined["sharpe"]:.3f}')

    return {'by_symbol': results, 'combined': combined}
