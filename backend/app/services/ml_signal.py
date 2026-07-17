"""
Phase 5 — ML 信号实时推断（深度版）

每根 5m bar 收盘时：
  1. 从 Parquet 增量读取最近 N 根 5m + 各高周期数据
  2. 计算特征向量（复用 feature_builder）
  3. 调用已训练 LGB+CB ensemble → long_prob / short_prob
  4. Kelly 公式计算最优仓位倍率
  5. 概念漂移检测（PSI 监控特征分布偏移）
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict
import warnings; warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════════════════════════════════
# 阈值配置加载（由 Phase 5 生成）
# ══════════════════════════════════════════════════════════════════════════════

_THRESHOLDS_CACHE: Optional[Dict] = None

# Per-symbol feature cache: symbol → (5m_bar_ts_sec, DataFrame)
# Cache is valid for the duration of one 5-minute bar; rebuilt only when bar timestamp advances.
_FEAT_CACHE: Dict[str, tuple] = {}

BAR_INTERVAL_SECONDS = 5 * 60
MAX_FEATURE_AGE_SECONDS = 15 * 60


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz='UTC')


def _normalize_open_times(values: pd.Series) -> pd.Series:
    """Normalize datetime-like or epoch-valued open times to UTC."""
    series = pd.Series(values, index=values.index)
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return pd.to_datetime(series, utc=True, errors='coerce')

    numeric = pd.to_numeric(series, errors='coerce')
    normalized = pd.Series(pd.NaT, index=series.index, dtype='datetime64[ns, UTC]')
    magnitude = numeric.abs()
    units = (
        ('ns', magnitude >= 1e17),
        ('us', (magnitude >= 1e14) & (magnitude < 1e17)),
        ('ms', (magnitude >= 1e11) & (magnitude < 1e14)),
        ('s', magnitude < 1e11),
    )
    for unit, mask in units:
        if mask.any():
            normalized.loc[mask] = pd.to_datetime(
                numeric.loc[mask], unit=unit, utc=True, errors='coerce'
            )

    datetime_mask = series.notna() & numeric.isna()
    if datetime_mask.any():
        normalized.loc[datetime_mask] = pd.to_datetime(
            series.loc[datetime_mask], utc=True, errors='coerce'
        )
    return normalized


def _normalize_timestamp(value) -> Optional[pd.Timestamp]:
    normalized = _normalize_open_times(pd.Series([value]))
    timestamp = normalized.iloc[0]
    return None if pd.isna(timestamp) else timestamp


def _completed_5m_open(now: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    current = _normalize_timestamp(_utc_now() if now is None else now)
    if current is None:
        raise ValueError('Current UTC time is invalid')
    return current.floor('5min') - pd.Timedelta(seconds=BAR_INTERVAL_SECONDS)


def _completed_feature_rows(
    frame: pd.DataFrame,
    completed_boundary: pd.Timestamp,
) -> pd.DataFrame:
    if 'open_time' not in frame.columns:
        raise ValueError('Feature frame has no open_time column')
    completed = frame.copy()
    completed['open_time'] = _normalize_open_times(completed['open_time'])
    completed = completed.dropna(subset=['open_time'])
    completed = completed[completed['open_time'] <= completed_boundary]
    return (
        completed.drop_duplicates(subset=['open_time'], keep='last')
        .sort_values('open_time')
        .reset_index(drop=True)
    )

def _load_thresholds() -> Dict:
    """读取 Phase 5 生成的 thresholds.json，缓存到模块级变量。"""
    global _THRESHOLDS_CACHE
    if _THRESHOLDS_CACHE is not None:
        return _THRESHOLDS_CACHE
    try:
        from ..datastore import resolve_current_model_release
        import json
        p = resolve_current_model_release() / 'thresholds.json'
        if p.exists():
            with open(p, encoding='utf-8') as f:
                _THRESHOLDS_CACHE = json.load(f)
            return _THRESHOLDS_CACHE
    except Exception:
        pass
    _THRESHOLDS_CACHE = {}
    return _THRESHOLDS_CACHE


def get_threshold(symbol: str, target: str,
                  mode: str = 'recommended') -> float:
    """
    获取单币单方向阈值。mode='recommended'|'thresh_f1'|'thresh_p65'
    找不到时返回默认 0.55。
    """
    thr_map = _load_thresholds()
    key = f'{symbol}_{target}'
    entry = thr_map.get(key, {})
    return float(entry.get(mode, 0.50))


# ══════════════════════════════════════════════════════════════════════════════
# 信号结果
# ══════════════════════════════════════════════════════════════════════════════

# Symbols that currently use a BTC-proxy model (trained on limited own-coin data).
# Signals from these symbols are exploratory — pos_size_mult is halved.
_PROXY_SYMBOLS: frozenset = frozenset({'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT'})


@dataclass
class SignalResult:
    symbol:          str
    long_prob:       float = 0.0
    short_prob:      float = 0.0
    action:          str   = 'observe'    # 'long' | 'short' | 'observe'
    confidence:      float = 0.0
    pos_size_mult:   float = 0.0
    threshold_used:  float = 0.55
    model_available: bool  = False
    drift_warning:   bool  = False        # PSI 超阈值时置 True
    proxy_model:     bool  = False        # BTC-proxy model; treat signal as exploratory
    feature_timestamp: Optional[str] = None
    data_age_seconds: Optional[float] = None
    feature_fresh:   bool  = False
    note:            str   = ''

    @property
    def best_direction(self) -> int:
        if self.action == 'long':  return  1
        if self.action == 'short': return -1
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Kelly 仓位计算
# ══════════════════════════════════════════════════════════════════════════════

def kelly_fraction(
    prob:       float,
    win_mult:   float = 2.0,   # TP = 2R
    loss_mult:  float = 1.0,   # SL = 1R
    kelly_frac: float = 0.25,  # 1/4 Kelly（保守）
    clip_max:   float = 1.5,
    clip_min:   float = 0.3,
) -> float:
    """
    Kelly 公式: f = (p*b - q) / b
    p = 胜率, q = 1-p, b = win_mult/loss_mult

    返回仓位倍率（相对于基础仓位）
    缩放到 [clip_min, clip_max]
    """
    if prob <= 0 or prob >= 1:
        return 1.0
    b = win_mult / loss_mult
    q = 1 - prob
    f = (prob * b - q) / b          # 全 Kelly
    f = max(0.0, f)                  # 负值截断
    f_scaled = f * kelly_frac        # 部分 Kelly
    # 映射到 [clip_min, clip_max]：full_kelly * kelly_frac → clip_max
    full = (0.5 * b - 0.5) / b * kelly_frac   # p=0.5 时的参考值
    if full > 0:
        mult = clip_min + (f_scaled / (full * 2)) * (clip_max - clip_min)
    else:
        mult = clip_min
    return float(np.clip(mult, clip_min, clip_max))


# ══════════════════════════════════════════════════════════════════════════════
# PSI 概念漂移检测
# ══════════════════════════════════════════════════════════════════════════════

def compute_psi(expected: np.ndarray, actual: np.ndarray,
                n_bins: int = 10) -> float:
    """
    Population Stability Index（PSI）
    < 0.10 : 稳定
    0.10~0.25 : 轻微漂移
    > 0.25 : 显著漂移
    """
    expected = expected[~np.isnan(expected)]
    actual   = actual[~np.isnan(actual)]
    if len(expected) < 20 or len(actual) < 20:
        return 0.0

    bins = np.percentile(expected, np.linspace(0, 100, n_bins + 1))
    bins[0]  -= 1e-9
    bins[-1] += 1e-9

    def _dist(arr):
        counts, _ = np.histogram(arr, bins=bins)
        dist = counts / len(arr)
        dist = np.where(dist == 0, 1e-6, dist)
        return dist

    e_dist = _dist(expected)
    a_dist = _dist(actual)
    return float(np.sum((a_dist - e_dist) * np.log(a_dist / e_dist)))


def check_drift(
    bundle,
    X_current: pd.DataFrame,
    ref_path:  Optional[Path] = None,
    psi_threshold: float = 0.25,
    top_k_feats:   int   = 20,
) -> Dict[str, float]:
    """
    对 top_k_feats 个特征计算 PSI，ref_path 是训练集特征统计（parquet）。
    返回 {feature: psi} dict。
    """
    if ref_path is None or not ref_path.exists():
        return {}

    try:
        ref = pd.read_parquet(ref_path)
    except Exception:
        return {}

    check_cols = bundle.shap_top20[:top_k_feats] if bundle.shap_top20 else \
                 bundle.feature_cols[:top_k_feats]

    psi_scores = {}
    for col in check_cols:
        if col not in ref.columns or col not in X_current.columns:
            continue
        psi = compute_psi(ref[col].values, X_current[col].values)
        psi_scores[col] = round(psi, 4)
    return psi_scores


# ══════════════════════════════════════════════════════════════════════════════
# 特征计算（最近 N 根 bar）
# ══════════════════════════════════════════════════════════════════════════════

def _current_5m_ts() -> int:
    """Return current UTC time floored to 5-minute boundary, in seconds."""
    now = _normalize_timestamp(_utc_now())
    if now is None:
        raise ValueError('Current UTC time is invalid')
    return int(now.floor('5min').timestamp())


def _latest_features(symbol: str, lookback_days: int = 30) -> Optional[pd.DataFrame]:
    """
    Returns feature DataFrame for the current 5m bar.
    Result is cached per symbol per 5-minute bar — rebuild only when bar timestamp advances.
    """
    bar_ts = _current_5m_ts()
    cached = _FEAT_CACHE.get(symbol)
    if cached is not None and cached[0] == bar_ts:
        return cached[1]

    try:
        from .feature_builder import build_features

        now = _normalize_timestamp(_utc_now())
        if now is None:
            return None
        completed_boundary = _completed_5m_open(now)
        start = (now - pd.Timedelta(days=lookback_days)).date().isoformat()
        end = (now + pd.Timedelta(days=1)).date().isoformat()

        df = build_features(symbol, start=start, end=end)
        if df is None:
            return None
        df = _completed_feature_rows(df, completed_boundary)
        if len(df) < 50:
            return None
        _FEAT_CACHE[symbol] = (bar_ts, df)
        return df
    except Exception as e:
        print(f'  _latest_features error [{symbol}]: {e}')
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 主信号函数
# ══════════════════════════════════════════════════════════════════════════════

def get_ml_signal(
    symbol:             str,
    long_threshold:     Optional[float] = None,
    short_threshold:    Optional[float] = None,
    min_confidence_gap: float = 0.05,
    lookback_days:      int   = 30,
    check_drift_flag:   bool  = True,
    threshold_mode:     str   = 'recommended',
    max_data_age_seconds: float = MAX_FEATURE_AGE_SECONDS,
) -> SignalResult:
    """
    计算当前 ML 信号。若模型未加载返回 observe。
    long_threshold / short_threshold 为 None 时自动从 thresholds.json 加载。
    threshold_mode: 'recommended' | 'thresh_f1' | 'thresh_p65'
    """
    from .trend_predictor import load_model, predict_proba as _predict
    from ..datastore import FEATURES_ML_DIR

    if long_threshold is None:
        long_threshold  = get_threshold(symbol, 'long_label',  threshold_mode)
    if short_threshold is None:
        short_threshold = get_threshold(symbol, 'short_label', threshold_mode)

    result = SignalResult(symbol=symbol, threshold_used=float(long_threshold))

    try:
        long_bundle  = load_model(symbol, 'long_label')
        short_bundle = load_model(symbol, 'short_label')
        result.model_available = True
    except FileNotFoundError:
        result.note = '模型文件不存在，需先运行 run_ml_pipeline.py --phase 4'
        return result
    except Exception as e:
        result.note = f'模型加载失败: {e}'
        return result

    if symbol in _PROXY_SYMBOLS:
        result.proxy_model = True

    # 计算特征
    feat_df = _latest_features(symbol, lookback_days)
    if feat_df is None or len(feat_df) == 0:
        result.note = '特征计算失败（数据不足）'
        return result

    feat_row = feat_df.iloc[-1]
    feature_ts = _normalize_timestamp(feat_row.get('open_time'))
    now = _normalize_timestamp(_utc_now())
    if feature_ts is None or now is None:
        result.note = 'Feature timestamp is invalid'
        return result

    result.feature_timestamp = feature_ts.isoformat().replace('+00:00', 'Z')
    result.data_age_seconds = round(float((now - feature_ts).total_seconds()), 3)
    completed_boundary = _completed_5m_open(now)
    if feature_ts > completed_boundary:
        result.note = (
            'Feature bar is incomplete: '
            f'{result.feature_timestamp} > {completed_boundary.isoformat()}'
        )
        return result
    if result.data_age_seconds > float(max_data_age_seconds):
        result.note = (
            f'Feature data is stale: age={result.data_age_seconds:.0f}s '
            f'> max={float(max_data_age_seconds):.0f}s'
        )
        return result
    result.feature_fresh = True

    try:
        lp = _predict(long_bundle,  feat_row)
        sp = _predict(short_bundle, feat_row)
    except Exception as e:
        result.note = f'推断失败: {e}'
        return result

    result.long_prob  = round(lp, 4)
    result.short_prob = round(sp, 4)
    result.confidence = round(max(lp, sp), 4)

    # 概念漂移检测（PSI）
    if check_drift_flag:
        ref_path = FEATURES_ML_DIR / f'{symbol}_ref_stats.parquet'
        psi_dict = check_drift(long_bundle, feat_df.tail(500), ref_path)
        if psi_dict:
            max_psi = max(psi_dict.values())
            if max_psi > 0.25:
                result.drift_warning = True
                result.note = f'WARN: 特征分布漂移 max_PSI={max_psi:.3f}'

    # 决策逻辑
    gap = abs(lp - sp)
    if lp >= long_threshold and lp > sp and gap >= min_confidence_gap:
        result.action = 'long'
    elif sp >= short_threshold and sp > lp and gap >= min_confidence_gap:
        result.action = 'short'
    else:
        result.action = 'observe'

    # Kelly 仓位倍率
    if result.action != 'observe':
        best_prob = max(lp, sp)
        result.pos_size_mult = kelly_fraction(
            best_prob, win_mult=2.0, loss_mult=1.0,
            kelly_frac=0.25, clip_max=1.5, clip_min=0.3,
        )
        if result.drift_warning:
            result.pos_size_mult *= 0.5   # 漂移时减半仓位
        if result.proxy_model:
            result.pos_size_mult *= 0.5   # 代理模型信号探索性，减半仓位
            result.note = (f'[proxy] {result.note}' if result.note
                           else '[proxy] BTC代理模型，信号仅供参考').strip()

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 批量扫描
# ══════════════════════════════════════════════════════════════════════════════

def scan_universe(
    symbols:         List[str],
    long_threshold:  float = 0.55,
    short_threshold: float = 0.55,
    check_drift:     bool  = True,
) -> List[SignalResult]:
    """扫描多个币种，返回有信号（非 observe）的结果列表，按 confidence 降序。"""
    results = []
    for sym in symbols:
        try:
            r = get_ml_signal(sym, long_threshold, short_threshold,
                              check_drift_flag=check_drift)
            results.append(r)
        except Exception as e:
            print(f'  scan_universe error [{sym}]: {e}')

    active = [r for r in results if r.action != 'observe']
    active.sort(key=lambda r: r.confidence, reverse=True)
    return active


# ══════════════════════════════════════════════════════════════════════════════
# 生成参考特征统计（用于 PSI 基准）
# ══════════════════════════════════════════════════════════════════════════════

def build_reference_stats(symbol: str, start: str = '2022-01-01',
                          end: str = '2025-12-31') -> None:
    """
    将训练期特征分布保存为参考文件，后续推断时与之对比 PSI。
    应在训练完成后调用一次。
    """
    from .feature_builder import build_features
    from ..datastore import FEATURES_ML_DIR

    print(f'  [{symbol}] 构建参考特征统计...')
    df = build_features(symbol, start=start, end=end)
    if df is None or len(df) == 0:
        print(f'  ERR: 无法构建参考统计')
        return

    out = FEATURES_ML_DIR
    out.mkdir(exist_ok=True)
    df.to_parquet(out / f'{symbol}_ref_stats.parquet', index=False)
    print(f'  参考统计保存 → {out / (symbol + "_ref_stats.parquet")}')
