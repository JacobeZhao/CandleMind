"""
Phase 3 — 多周期特征工程（深度版）

六大类特征，每类都有独立函数，最终 merge_asof 对齐到 5m 时间轴：

1. 价格结构   : 支撑阻力、枢轴点、摆动高低、K 线形态
2. 动量与趋势 : 多窗口动量、EMA 对齐得分、趋势加速度
3. 波动率体系 : ATR 分位、波动率机制、vol-of-vol、GARCH 残差
4. 成交量资金 : OBV、VWAP 偏离、量价背离、资金费率
5. 统计特征   : Hurst 指数、自相关、熵值、峰度偏度、方差比
6. 跨资产     : BTC Beta、滚动相关、相对强弱

时间特征 : 小时/星期的正弦余弦编码（非对称市场效应）

所有特征 merge_asof(direction='backward')，严格无前视。
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Tuple, Optional, List
import warnings; warnings.filterwarnings('ignore')

try:
    from numba import njit as _njit
    _NUMBA = True
except ImportError:
    def _njit(fn):          # 无 numba 时透明降级
        return fn
    _NUMBA = False


# ══════════════════════════════════════════════════════════════════════════════
# 基础指标工具
# ══════════════════════════════════════════════════════════════════════════════

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rma(s: pd.Series, n: int) -> pd.Series:           # Wilder 平滑
    return s.ewm(alpha=1 / n, adjust=False).mean()

def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low']  - df['close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return _rma(tr, n)

def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, down = df['high'].diff(), -df['low'].diff()
    pdm = np.where((up > down) & (up > 0), up, 0.0)
    ndm = np.where((down > up) & (down > 0), down, 0.0)
    atr = _atr(df, n)
    pdi = 100 * _rma(pd.Series(pdm, index=df.index), n) / atr.replace(0, np.nan)
    ndi = 100 * _rma(pd.Series(ndm, index=df.index), n) / atr.replace(0, np.nan)
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return _rma(dx.fillna(0), n)

def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    return 100 - 100 / (1 + _rma(d.clip(lower=0), n) / _rma((-d).clip(lower=0), n).replace(0, np.nan))

def _mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Money Flow Index"""
    tp  = (df['high'] + df['low'] + df['close']) / 3
    rmf = tp * df['volume']
    pos = rmf.where(tp > tp.shift(1), 0).rolling(n).sum()
    neg = rmf.where(tp < tp.shift(1), 0).rolling(n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))

def _obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df['close'].diff().fillna(0))
    return (sign * df['volume']).cumsum()

def _vwap(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df['high'] + df['low'] + df['close']) / 3
    return (tp * df['volume']).rolling(n).sum() / df['volume'].rolling(n).sum()

def _stoch_rsi(s: pd.Series, rsi_period: int = 14, stoch_period: int = 14) -> pd.Series:
    rsi = _rsi(s, rsi_period)
    lo  = rsi.rolling(stoch_period).min()
    hi  = rsi.rolling(stoch_period).max()
    return (rsi - lo) / (hi - lo).replace(0, np.nan)


# ══════════════════════════════════════════════════════════════════════════════
# 1. 价格结构特征
# ══════════════════════════════════════════════════════════════════════════════

def price_structure_features(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """
    支撑阻力、枢轴点、区间位置、价格形态
    """
    d     = df.copy()
    close = d['close']
    high  = d['high']
    low   = d['low']
    atr   = _atr(d)
    f     = pd.DataFrame({'open_time': d['open_time']})

    # ── 枢轴点（前一根 bar 的 H/L/C 计算）──
    pivot = (high.shift(1) + low.shift(1) + close.shift(1)) / 3
    r1    = 2 * pivot - low.shift(1)
    s1    = 2 * pivot - high.shift(1)
    f[f'{prefix}_dist_pivot_r'] = (close - pivot) / atr          # 离枢轴距离（R）
    f[f'{prefix}_dist_r1_r']    = (close - r1)    / atr
    f[f'{prefix}_dist_s1_r']    = (close - s1)    / atr

    # ── 多窗口区间高低位置 ──
    for w in [10, 20, 50]:
        h = high.rolling(w).max()
        l = low.rolling(w).min()
        r = (h - l).replace(0, np.nan)
        f[f'{prefix}_pos_{w}']      = (close - l) / r        # [0,1]：在区间中的位置
        f[f'{prefix}_range_pct_{w}'] = r / close * 100       # 区间宽度 %

    # ── 摆动高低点（简化版：n bar 内新高/新低）──
    for w in [5, 10]:
        f[f'{prefix}_new_high_{w}'] = (high == high.rolling(w).max()).astype(float)
        f[f'{prefix}_new_low_{w}']  = (low  == low.rolling(w).min()).astype(float)

    # ── K 线形态特征 ──
    body    = (close - d['open']).abs()
    candle  = high - low
    f[f'{prefix}_body_ratio']    = body / candle.replace(0, np.nan)   # 实体比例
    upper_shadow = high - pd.concat([close, d['open']], axis=1).max(axis=1)
    lower_shadow = pd.concat([close, d['open']], axis=1).min(axis=1) - low
    f[f'{prefix}_upper_shadow_r'] = upper_shadow / atr
    f[f'{prefix}_lower_shadow_r'] = lower_shadow / atr
    f[f'{prefix}_is_bullish']     = (close > d['open']).astype(float)

    # ── 跳空 ──
    f[f'{prefix}_gap_r'] = (d['open'] - close.shift(1)) / atr        # 开盘跳空（ATR 归一化）

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 2. 动量与趋势特征
# ══════════════════════════════════════════════════════════════════════════════

def momentum_features(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    d     = df.copy()
    close = d['close']
    atr   = _atr(d)
    f     = pd.DataFrame({'open_time': d['open_time']})

    # ── 多窗口收益率动量 ──
    for w in [3, 6, 12, 24, 48, 96]:
        ret = close.pct_change(w)
        f[f'{prefix}_ret_{w}']  = ret
        f[f'{prefix}_ret_{w}_z'] = (ret - ret.rolling(100).mean()) / ret.rolling(100).std()

    # ── EMA 对齐得分（多空方向评分 -3~+3）──
    emas = [_ema(close, p) for p in [8, 21, 55]]
    align = sum(
        (emas[i] > emas[j]).astype(int) * 2 - 1
        for i in range(len(emas)) for j in range(i + 1, len(emas))
    )
    f[f'{prefix}_ema_align_score'] = align  # -3 = 全空排, +3 = 全多排

    # ── EMA 斜率 & 加速度 ──
    for p in [8, 21]:
        ema_p = _ema(close, p)
        slope = (ema_p - ema_p.shift(3)) / atr           # 斜率（R/3bars）
        f[f'{prefix}_ema{p}_slope']  = slope
        f[f'{prefix}_ema{p}_accel']  = slope - slope.shift(3)    # 加速度

    # ── 价格离 EMA 的距离 ──
    for p in [21, 55, 200]:
        ema_p = _ema(close, p)
        f[f'{prefix}_dist_ema{p}_r'] = (close - ema_p) / atr

    # ── ADX 及方向指标 ──
    f[f'{prefix}_adx']  = _adx(d)
    f[f'{prefix}_rsi']  = _rsi(close)
    f[f'{prefix}_srsi'] = _stoch_rsi(close)

    # ── MACD ──
    macd = _ema(close, 12) - _ema(close, 26)
    sig  = _ema(macd, 9)
    f[f'{prefix}_macd_hist_r']  = (macd - sig) / atr
    f[f'{prefix}_macd_slope']   = macd.diff(3) / atr

    # ── ROC 加速度 ──
    roc6  = close.pct_change(6)
    f[f'{prefix}_roc_accel'] = roc6 - roc6.shift(6)

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 3. 波动率体系特征
# ══════════════════════════════════════════════════════════════════════════════

def volatility_features(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    d     = df.copy()
    close = d['close']
    atr   = _atr(d)
    f     = pd.DataFrame({'open_time': d['open_time']})

    # ── ATR 相对分位 ──
    for w in [20, 50, 100]:
        q = atr.rolling(w).rank(pct=True)
        f[f'{prefix}_atr_pct{w}']    = atr / close * 100       # ATR%
        f[f'{prefix}_atr_qrank{w}']  = q                        # ATR 分位 [0,1]

    # ── 布林带 ──
    for w in [20, 50]:
        m  = close.rolling(w).mean()
        sd = close.rolling(w).std()
        f[f'{prefix}_bb_width_{w}']  = (4 * sd) / m.replace(0, np.nan)
        f[f'{prefix}_bb_pos_{w}']    = (close - (m - 2*sd)) / (4*sd).replace(0, np.nan)

    # ── 已实现波动率（对数收益标准差）──
    log_ret = np.log(close / close.shift(1))
    for w in [12, 24, 48]:
        rv = log_ret.rolling(w).std() * np.sqrt(w)
        f[f'{prefix}_rv{w}'] = rv

    # ── Vol-of-vol（波动率的波动率）──
    rv24 = log_ret.rolling(24).std()
    f[f'{prefix}_vol_of_vol'] = rv24.rolling(24).std() / rv24.rolling(24).mean().replace(0, np.nan)

    # ── 波动率机制（低/中/高档位，基于 100-bar 分位）──
    rv_q = rv24.rolling(100, min_periods=50).rank(pct=True)
    f[f'{prefix}_vol_regime'] = pd.cut(rv_q, bins=[0, 0.33, 0.67, 1.0],
                                        labels=[0, 1, 2], include_lowest=True).astype(float)

    # ── GARCH(1,1) 残差（简化：用条件方差代理）──
    sq_ret = log_ret ** 2
    garch_var = sq_ret.ewm(span=10).mean()
    f[f'{prefix}_garch_resid'] = (sq_ret - garch_var) / garch_var.replace(0, np.nan)

    # ── Parkinson 波动率（用高低价，更准确）──
    park = (np.log(d['high'] / d['low']) ** 2 / (4 * np.log(2)))
    f[f'{prefix}_park_vol'] = park.rolling(20).mean().apply(np.sqrt)

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 4. 成交量与资金特征
# ══════════════════════════════════════════════════════════════════════════════

def volume_features(df: pd.DataFrame, prefix: str,
                    funding_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    d     = df.copy()
    close = d['close']
    vol   = d['volume']
    atr   = _atr(d)
    f     = pd.DataFrame({'open_time': d['open_time']})

    # ── 成交量比（相对均值）──
    for w in [12, 24, 48]:
        vm = vol.rolling(w).mean()
        f[f'{prefix}_vol_ratio_{w}'] = vol / vm.replace(0, np.nan)

    # ── OBV 动量 ──
    obv = _obv(d)
    f[f'{prefix}_obv_slope']  = (obv - obv.shift(12)) / vol.rolling(12).sum().replace(0, np.nan)
    f[f'{prefix}_obv_z']      = (obv - obv.rolling(50).mean()) / obv.rolling(50).std()

    # ── VWAP 偏离 ──
    vwap = _vwap(d, 20)
    f[f'{prefix}_vwap_dev_r'] = (close - vwap) / atr

    # ── MFI ──
    f[f'{prefix}_mfi'] = _mfi(d)

    # ── A/D Line（累积分布）──
    clv = ((close - d['low']) - (d['high'] - close)) / (d['high'] - d['low']).replace(0, np.nan)
    adl = (clv * vol).cumsum()
    f[f'{prefix}_adl_slope'] = (adl - adl.shift(12)) / vol.rolling(12).sum().replace(0, np.nan)

    # ── 量价背离（价格新高但量能萎缩）──
    price_high = (close == close.rolling(20).max()).astype(float)
    low_vol    = (vol < vol.rolling(20).mean() * 0.7).astype(float)
    f[f'{prefix}_price_vol_diverge'] = price_high * low_vol   # 1 = 顶背离信号

    # ── Taker Buy Ratio（若数据有该列）──
    if 'taker_buy_base' in d.columns and 'volume' in d.columns:
        tbr = d['taker_buy_base'].astype(float) / vol.replace(0, np.nan)
        f[f'{prefix}_taker_buy_ratio'] = tbr
        # 3天z-score（5m×288=1天）：捕捉主动买力异常涌入
        tbr_mean = tbr.rolling(288, min_periods=48).mean()
        tbr_std  = tbr.rolling(288, min_periods=48).std().replace(0, np.nan)
        f[f'{prefix}_taker_buy_z_288'] = (tbr - tbr_mean) / tbr_std
        # 连续主动买占优 streak（tbr>0.52 视为主动买主导）
        dominant = (tbr > 0.52).astype(int)
        grp = (dominant != dominant.shift()).cumsum()
        f[f'{prefix}_taker_buy_streak'] = (
            dominant * (dominant.groupby(grp).cumcount() + 1)
        )

    # ── 资金费率（合约特有）──
    if funding_df is not None and len(funding_df) > 0:
        # funding_df 列名：funding_time(ms), rate
        fd_cols = funding_df.columns.tolist()
        time_col = 'funding_time' if 'funding_time' in fd_cols else 'open_time'
        rate_col = 'rate' if 'rate' in fd_cols else 'funding_rate'
        fd = funding_df[[time_col, rate_col]].copy()
        fd = fd.rename(columns={time_col: 'open_time', rate_col: f'{prefix}_funding_rate'})
        fd['open_time'] = pd.to_numeric(fd['open_time'])
        f = f.reset_index()
        f['open_time'] = pd.to_numeric(f['open_time'])
        f = pd.merge_asof(f.sort_values('open_time'),
                          fd.sort_values('open_time'),
                          on='open_time', direction='backward')
        f = f.set_index('open_time')
        # 派生资金费率特征（无需 close，纯费率序列）
        fr = f[f'{prefix}_funding_rate'].fillna(0)
        fr_30d_mean = fr.rolling(90, min_periods=30).mean()
        fr_30d_std  = fr.rolling(90, min_periods=30).std().replace(0, np.nan)
        fr_3d_mean  = fr.rolling(9,  min_periods=3).mean()
        f[f'{prefix}_funding_z3d']           = (fr_3d_mean - fr_30d_mean) / fr_30d_std
        f[f'{prefix}_funding_crowded_long']  = (fr > fr_30d_mean + fr_30d_std).astype(float)
        f[f'{prefix}_funding_crowded_short'] = (fr < fr_30d_mean - fr_30d_std).astype(float)

    return f.set_index('open_time') if 'open_time' in f.columns else f


# ══════════════════════════════════════════════════════════════════════════════
# 5. 统计特征
# ══════════════════════════════════════════════════════════════════════════════

@_njit
def _hurst_rs(arr: np.ndarray) -> float:
    """R/S 法 Hurst 指数（Numba JIT）"""
    n = len(arr)
    if n < 20:
        return np.nan
    # 用三个 lag 尺度：n//2, n//4, n//8
    rs_vals  = np.empty(3)
    lag_vals = np.empty(3)
    count = 0
    for k in (2, 4, 8):
        lag = n // k
        if lag < 4:
            continue
        n_chunks = n // lag
        if n_chunks == 0:
            continue
        rs_sum   = 0.0
        rs_count = 0
        for ci in range(n_chunks):
            c      = arr[ci * lag:(ci + 1) * lag]
            mean_c = np.mean(c)
            std_c  = np.std(c)
            if std_c == 0.0:
                continue
            # 手动 cumsum max/min（避免分配临时数组）
            cumsum = 0.0
            c_max  = -1e18
            c_min  =  1e18
            for v in c:
                cumsum += v - mean_c
                if cumsum > c_max:
                    c_max = cumsum
                if cumsum < c_min:
                    c_min = cumsum
            rs_sum   += (c_max - c_min) / std_c
            rs_count += 1
        if rs_count > 0:
            rs_vals[count]  = np.log(rs_sum / rs_count)
            lag_vals[count] = np.log(float(lag))
            count += 1
    if count < 2:
        return np.nan
    # 手动线性回归（polyfit 替代）
    x = lag_vals[:count]
    y = rs_vals[:count]
    n_pts  = float(count)
    sum_x  = 0.0; sum_y = 0.0; sum_xx = 0.0; sum_xy = 0.0
    for i in range(count):
        sum_x  += x[i]
        sum_y  += y[i]
        sum_xx += x[i] * x[i]
        sum_xy += x[i] * y[i]
    denom = n_pts * sum_xx - sum_x * sum_x
    if denom == 0.0:
        return np.nan
    return (n_pts * sum_xy - sum_x * sum_y) / denom


@_njit
def _variance_ratio(arr: np.ndarray, q: int = 4) -> float:
    """方差比检验（Numba JIT）"""
    n = len(arr)
    if n < q * 2 + 1:
        return np.nan
    # 对数收益
    ret = np.empty(n - 1)
    for i in range(n - 1):
        v = arr[i + 1] / (arr[i] + 1e-10)
        ret[i] = np.log(v) if v > 0 else 0.0
    # var(ret, ddof=1)
    mean1 = np.mean(ret)
    var1  = 0.0
    for v in ret:
        var1 += (v - mean1) ** 2
    if len(ret) <= 1:
        return np.nan
    var1 /= (len(ret) - 1)
    if var1 == 0.0:
        return np.nan
    # var(ret[::q], ddof=1)
    ret_q = ret[::q]
    if len(ret_q) <= 1:
        return np.nan
    mean_q = np.mean(ret_q)
    var_q  = 0.0
    for v in ret_q:
        var_q += (v - mean_q) ** 2
    var_q /= (len(ret_q) - 1)
    return var_q / var1


@_njit
def _autocorr_njit(arr: np.ndarray, lag: int) -> float:
    """手动自相关（Numba JIT）"""
    n = len(arr)
    if n <= lag:
        return np.nan
    x = arr[:n - lag]
    y = arr[lag:]
    mean_x = np.mean(x)
    mean_y = np.mean(y)
    cov    = 0.0
    ss_x   = 0.0
    ss_y   = 0.0
    for i in range(len(x)):
        dx = x[i] - mean_x
        dy = y[i] - mean_y
        cov  += dx * dy
        ss_x += dx * dx
        ss_y += dy * dy
    denom = np.sqrt(ss_x * ss_y)
    if denom == 0.0:
        return 0.0
    return cov / denom


@_njit
def _rsq_njit(arr: np.ndarray) -> float:
    """线性回归 R²（Numba JIT）"""
    n = len(arr)
    if n < 5:
        return np.nan
    sum_x  = 0.0; sum_y = 0.0; sum_xx = 0.0; sum_xy = 0.0
    for i in range(n):
        sum_x  += i
        sum_y  += arr[i]
        sum_xx += i * i
        sum_xy += i * arr[i]
    fn     = float(n)
    denom  = fn * sum_xx - sum_x * sum_x
    if denom == 0.0:
        return np.nan
    slope  = (fn * sum_xy - sum_x * sum_y) / denom
    interc = (sum_y - slope * sum_x) / fn
    mean_y = sum_y / fn
    ss_res = 0.0; ss_tot = 0.0
    for i in range(n):
        pred    = slope * i + interc
        ss_res += (arr[i] - pred) ** 2
        ss_tot += (arr[i] - mean_y) ** 2
    if ss_tot == 0.0:
        return np.nan
    return 1.0 - ss_res / ss_tot


# 固定 lag 的自相关包装函数（engine='numba' 要求单参数）
@_njit
def _autocorr_1(x): return _autocorr_njit(x, 1)
@_njit
def _autocorr_3(x): return _autocorr_njit(x, 3)
@_njit
def _autocorr_6(x): return _autocorr_njit(x, 6)

# variance_ratio 固定 q=4
@_njit
def _vr4(x): return _variance_ratio(x, 4)


def statistical_features(df: pd.DataFrame, prefix: str,
                          hurst_window: int = 200) -> pd.DataFrame:
    d     = df.copy()
    close = d['close']
    log_r = np.log(close / close.shift(1))
    f     = pd.DataFrame({'open_time': d['open_time']})

    _engine = 'numba' if _NUMBA else None

    # ── Hurst 指数（滚动，Numba JIT）──
    f[f'{prefix}_hurst'] = close.rolling(hurst_window, min_periods=50).apply(
        _hurst_rs, raw=True, engine=_engine
    )

    # ── 方差比（VR4，Numba JIT）──
    f[f'{prefix}_vr4'] = close.rolling(60, min_periods=20).apply(
        _vr4, raw=True, engine=_engine
    )

    # ── 自相关（滞后 1/3/6，Numba JIT）──
    f[f'{prefix}_autocorr_1'] = log_r.rolling(200, min_periods=30).apply(
        _autocorr_1, raw=True, engine=_engine
    )
    f[f'{prefix}_autocorr_3'] = log_r.rolling(200, min_periods=30).apply(
        _autocorr_3, raw=True, engine=_engine
    )
    f[f'{prefix}_autocorr_6'] = log_r.rolling(200, min_periods=30).apply(
        _autocorr_6, raw=True, engine=_engine
    )

    # ── 峰度 & 偏度（内置，本来就快）──
    f[f'{prefix}_skew'] = log_r.rolling(200).skew()
    f[f'{prefix}_kurt'] = log_r.rolling(200).kurt()

    # ── 熵代理（标准差比值，无需 JIT）──
    f[f'{prefix}_entropy_proxy'] = (
        log_r.rolling(20).std() /
        log_r.rolling(100).std().replace(0, np.nan)
    )

    # ── 趋势强度 R²（Numba JIT）──
    for w in [20, 50]:
        f[f'{prefix}_trend_r2_{w}'] = close.rolling(w, min_periods=int(w * 0.8)).apply(
            _rsq_njit, raw=True, engine=_engine
        )

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 6. 跨资产特征（相对 BTC）
# ══════════════════════════════════════════════════════════════════════════════

def cross_asset_features(
    df:      pd.DataFrame,
    btc_df:  pd.DataFrame,
    prefix:  str,
    windows: Tuple = (24, 72, 168),   # 对应 1h 下 1天/3天/7天
) -> pd.DataFrame:
    """
    计算当前资产相对 BTC 的 Beta、相关性、相对强弱。
    btc_df 需与 df 同周期，使用 merge_asof 对齐。
    """
    d   = df.copy().sort_values('open_time')
    btc = btc_df.copy().sort_values('open_time')

    # 对齐 BTC 到当前 df 的时间戳
    merged = pd.merge_asof(
        d[['open_time', 'close']].rename(columns={'close': 'close_sym'}),
        btc[['open_time', 'close']].rename(columns={'close': 'close_btc'}),
        on='open_time', direction='backward'
    )
    ret_sym = np.log(merged['close_sym'] / merged['close_sym'].shift(1))
    ret_btc = np.log(merged['close_btc'] / merged['close_btc'].shift(1))

    f = pd.DataFrame({'open_time': merged['open_time']})

    for w in windows:
        cov  = ret_sym.rolling(w).cov(ret_btc)
        var  = ret_btc.rolling(w).var()
        corr = ret_sym.rolling(w).corr(ret_btc)
        beta = cov / var.replace(0, np.nan)

        f[f'{prefix}_beta_btc_{w}']    = beta
        f[f'{prefix}_corr_btc_{w}']    = corr

        # 相对强弱：sym 累计收益 - BTC 累计收益（均为对数收益之和）
        f[f'{prefix}_rel_strength_{w}'] = (
            ret_sym.rolling(w).sum() - ret_btc.rolling(w).sum()
        )

    # Alpha：残差动量（beta 调整后的超额收益）
    beta24 = f[f'{prefix}_beta_btc_24']
    alpha  = ret_sym - beta24 * ret_btc
    f[f'{prefix}_alpha_24']    = alpha.rolling(24).sum()
    f[f'{prefix}_alpha_trend'] = (alpha.rolling(24).sum() - alpha.rolling(72).sum().shift(24))

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 7. 时间特征（非线性编码）
# ══════════════════════════════════════════════════════════════════════════════

def time_features(df: pd.DataFrame) -> pd.DataFrame:
    d  = df.copy()
    ts = pd.to_datetime(d['open_time'], unit='ms', utc=True)
    f  = pd.DataFrame({'open_time': d['open_time']})

    # 小时（周期编码）
    hour = ts.dt.hour
    f['hour_sin']    = np.sin(2 * np.pi * hour / 24)
    f['hour_cos']    = np.cos(2 * np.pi * hour / 24)

    # 星期（周期编码）
    dow = ts.dt.dayofweek
    f['dow_sin']     = np.sin(2 * np.pi * dow / 7)
    f['dow_cos']     = np.cos(2 * np.pi * dow / 7)

    # 月内日期
    dom = ts.dt.day
    f['dom_sin']     = np.sin(2 * np.pi * dom / 31)
    f['dom_cos']     = np.cos(2 * np.pi * dom / 31)

    # 亚洲/欧洲/美洲盘标志（UTC 时间）
    f['session_asia'] = ((hour >= 0) & (hour < 8)).astype(float)
    f['session_eu']   = ((hour >= 7) & (hour < 15)).astype(float)
    f['session_us']   = ((hour >= 13) & (hour < 22)).astype(float)

    return f.set_index('open_time')


# ══════════════════════════════════════════════════════════════════════════════
# 交叉特征（类别交互）
# ══════════════════════════════════════════════════════════════════════════════

def interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    对已合并的宽表，生成高信息量的交叉特征
    df 必须已包含各周期特征列（merge 后调用）
    """
    f = pd.DataFrame(index=df.index)

    def _safe(col):
        return df[col] if col in df.columns else pd.Series(0, index=df.index)

    # ADX × 动量方向（强趋势 × 动量正负）
    adx1h   = _safe('1h_adx')
    ret1h   = _safe('1h_ret_6')
    f['ia_adx_mom_1h'] = adx1h * np.sign(ret1h)

    # 量比 × ATR 分位（放量扩波）
    vr1h  = _safe('1h_vol_ratio_12')
    atrq1h = _safe('1h_atr_qrank20')
    f['ia_vol_atr_1h'] = vr1h * atrq1h

    # BTC 相关性 × 相对强弱（脱钩后的独立行情）
    corr = _safe('1h_corr_btc_24')
    rs   = _safe('1h_rel_strength_24')
    f['ia_decouple_1h'] = (1 - corr.abs()) * rs

    # Hurst > 0.55（趋势性强）× ADX > 25（趋势明确）
    hurst4h = _safe('4h_hurst')
    adx4h   = _safe('4h_adx')
    f['ia_trend_confirm_4h'] = (
        (hurst4h > 0.55).astype(float) * (adx4h > 25).astype(float)
    )

    # 短期 RSI 极端值 × 中期方向
    rsi5m = _safe('5m_rsi')
    ret4h = _safe('4h_ret_6')
    f['ia_rsi_extreme_5m'] = (
        ((rsi5m < 30) | (rsi5m > 70)).astype(float) * np.sign(ret4h)
    )

    return f


# ══════════════════════════════════════════════════════════════════════════════
# 特征稳定性检验
# ══════════════════════════════════════════════════════════════════════════════

def feature_stability(df: pd.DataFrame, year_col: str = 'year') -> pd.DataFrame:
    """
    按年度分组，计算每个特征与 long_label/short_label 的 Spearman 相关系数，
    评估跨时间稳定性。返回特征稳定性报告。
    """
    from scipy.stats import spearmanr

    if year_col not in df.columns:
        df = df.copy()
        df[year_col] = pd.to_datetime(df['open_time'], unit='ms').dt.year

    feat_cols  = [c for c in df.columns if c not in
                  {'open_time', 'year', 'long_label', 'short_label',
                   'long_meta_label', 'short_meta_label', 'best_direction'}
                  and not c.startswith('long_') and not c.startswith('short_')]

    years  = sorted(df[year_col].unique())
    rows   = []
    for fc in feat_cols:
        corrs_l, corrs_s = [], []
        for yr in years:
            sub = df[df[year_col] == yr].dropna(subset=[fc, 'long_label'])
            if len(sub) < 100:
                continue
            cl, _ = spearmanr(sub[fc], sub['long_label'])
            cs, _ = spearmanr(sub[fc], sub['short_label'])
            corrs_l.append(cl)
            corrs_s.append(cs)
        if not corrs_l:
            continue
        rows.append({
            'feature':        fc,
            'corr_long_mean': round(np.mean(corrs_l),  4),
            'corr_long_std':  round(np.std(corrs_l),   4),
            'corr_short_mean':round(np.mean(corrs_s),  4),
            'corr_short_std': round(np.std(corrs_s),   4),
            'stable':         np.std(corrs_l) < 0.15 and abs(np.mean(corrs_l)) > 0.02,
        })

    return pd.DataFrame(rows).sort_values('corr_long_mean', key=abs, ascending=False)


# ══════════════════════════════════════════════════════════════════════════════
# 主入口：构建完整特征矩阵
# ══════════════════════════════════════════════════════════════════════════════

HIGHER_TFS = ('30m', '1h', '4h', '6h', '1d')


def build_features(
    symbol:     str,
    start:      str  = '2022-01-01',
    end:        str  = '2026-06-08',
    base_tf:    str  = '5m',
    higher_tfs: Tuple = HIGHER_TFS,
    with_stats: bool = True,
    with_cross: bool = True,
) -> pd.DataFrame:
    """
    全量特征构建，合并 6 大类特征到 5m 时间轴。
    """
    from .datafeed import load_klines

    print(f'  [{symbol}] 加载 {base_tf} 基础数据...')
    base = load_klines(symbol, base_tf, start, end)
    # 去重 + 排序，确保整数索引连续
    base = base.drop_duplicates(subset=['open_time']).sort_values('open_time').reset_index(drop=True)
    open_time_col = base['open_time'].values.copy()   # 保存时间轴，后面用于对齐
    n = len(base)

    # ── 资金费率（合约永续特有）──
    funding_df = None
    try:
        from .datafeed import load_funding
        funding_df = load_funding(symbol, start, end)
        if not funding_df.empty:
            print(f'  [{symbol}] 资金费率 {len(funding_df)} 条')
    except Exception as _fe:
        print(f'  [{symbol}] funding 跳过: {_fe}')

    def _align_to_base(feat_df: pd.DataFrame, shift_bars: int = 1) -> pd.DataFrame:
        """
        把任意特征 DataFrame 对齐到 base 的整数行索引（merge_asof → reset_index）。
        shift_bars=1：对所有非时间戳列执行 shift(1)，确保当前 5m bar 只能读取
        上一根已完全收盘的高周期 bar 的特征，消除 bar 内前视泄漏。
        """
        df = feat_df.reset_index() if feat_df.index.name == 'open_time' else feat_df.copy()
        if shift_bars > 0:
            feat_cols = [c for c in df.columns if c != 'open_time']
            df[feat_cols] = df[feat_cols].shift(shift_bars)
        merged = pd.merge_asof(
            pd.DataFrame({'open_time': open_time_col}),
            df,
            on='open_time', direction='backward'
        ).drop(columns=['open_time'])
        merged.index = range(n)
        return merged

    # ── 5m 自身特征（所有类别，直接用整数索引）──
    print(f'  [{symbol}] 计算 5m 特征...')

    def _strip_ot(df):
        """feature 函数返回的 DataFrame 去掉 open_time 索引/列，用整数索引"""
        if df.index.name == 'open_time':
            return df.reset_index(drop=True)
        return df.drop(columns=['open_time'], errors='ignore').reset_index(drop=True)

    parts = []
    parts.append(_strip_ot(price_structure_features(base, '5m')))
    parts.append(_strip_ot(momentum_features(base, '5m')))
    parts.append(_strip_ot(volatility_features(base, '5m')))
    parts.append(_strip_ot(volume_features(base, '5m', funding_df=funding_df)))
    if with_stats:
        parts.append(_strip_ot(statistical_features(base, '5m', hurst_window=200)))
    parts.append(_strip_ot(time_features(base)))

    # ── 高周期特征（merge_asof 对齐到 base 整数行）──
    for tf in higher_tfs:
        print(f'  [{symbol}] 加载 & 计算 {tf} 特征...')
        try:
            df_tf = load_klines(symbol, tf, start, end)
            df_tf = df_tf.drop_duplicates('open_time').sort_values('open_time').reset_index(drop=True)
            ps = price_structure_features(df_tf, tf).reset_index()
            mo = momentum_features(df_tf, tf).reset_index()
            vl = volatility_features(df_tf, tf).reset_index()
            vu = volume_features(df_tf, tf).reset_index()
            tf_combined = pd.concat([ps, mo, vl, vu], axis=1)
            # 去掉重复 open_time 列
            tf_combined = tf_combined.loc[:, ~tf_combined.columns.duplicated()]
            parts.append(_align_to_base(tf_combined))

            if with_stats and tf in ('1h', '4h', '1d'):
                stat_f = statistical_features(df_tf, tf, hurst_window=100).reset_index()
                parts.append(_align_to_base(stat_f))
        except Exception as e:
            print(f'  WARN {symbol} {tf}: {e}')

    # ── 跨资产特征（非 BTC 才计算）──
    if with_cross and symbol != 'BTCUSDT':
        print(f'  [{symbol}] 计算跨资产（vs BTC）特征...')
        try:
            btc_1h = load_klines('BTCUSDT', '1h', start, end)
            sym_1h = load_klines(symbol, '1h', start, end)
            ca_f   = cross_asset_features(sym_1h, btc_1h, prefix='1h').reset_index()
            parts.append(_align_to_base(ca_f))
        except Exception as e:
            print(f'  WARN cross-asset {symbol}: {e}')

    # ── 合并（全部整数索引，不会产生笛卡尔积）──
    result = pd.concat(parts, axis=1)
    result.insert(0, 'open_time', open_time_col)

    # ── 交叉特征 ──
    ia = interaction_features(result).reset_index(drop=True)
    result = pd.concat([result, ia], axis=1)

    # 去掉重复列名
    result = result.loc[:, ~result.columns.duplicated()]

    print(f'  [{symbol}] 特征维度: {result.shape}')
    return result


def build_and_save(symbol: str, start: str = '2022-01-01', end: str = '2026-06-08') -> pd.DataFrame:
    from ..datastore import MARKET_ROOT
    feats   = build_features(symbol, start, end)
    out_dir = MARKET_ROOT / 'features_ml'
    out_dir.mkdir(exist_ok=True)
    path    = out_dir / f'{symbol}_features.parquet'
    feats.to_parquet(path, index=False)
    print(f'  保存完成 → {path} ({feats.shape})')
    return feats


def _read_parquet_f32(path) -> pd.DataFrame:
    """分批读取 Parquet，float64 → float32，峰值内存减半。"""
    import pyarrow.parquet as pq
    import pyarrow as pa
    pf = pq.ParquetFile(path)
    chunks = []
    for batch in pf.iter_batches(batch_size=60_000):
        cols = []
        for i, field in enumerate(batch.schema):
            arr = batch.column(i)
            if pa.types.is_floating(field.type):
                arr = arr.cast(pa.float32())
            cols.append(arr)
        chunks.append(pa.RecordBatch.from_arrays(cols, names=batch.schema.names))
    return pa.Table.from_batches(chunks).to_pandas()


def merge_features_labels(symbol: str) -> pd.DataFrame:
    """加载特征 + 标注，合并、去 NaN、去预热期，返回建模用 DataFrame"""
    from ..datastore import MARKET_ROOT
    feat_path  = MARKET_ROOT / 'features_ml' / f'{symbol}_features.parquet'
    label_path = MARKET_ROOT / 'labels'      / f'{symbol}_5m_labels.parquet'

    feats  = _read_parquet_f32(feat_path)
    labels = pd.read_parquet(label_path)[[
        'open_time',
        'long_label',   'long_meta_label',  'long_profit_r',
        'short_label',  'short_meta_label', 'short_profit_r',
        'long_duration_bars', 'short_duration_bars',
        'long_trend_sharpe', 'short_trend_sharpe',
        'best_direction',
    ]]

    merged = feats.merge(labels, on='open_time', how='inner')
    # 去掉指标预热期（前 200 bar）和最后无标注区段（后 48 bar）
    merged = merged.iloc[200:-48].reset_index(drop=True)
    # 丢弃高缺失列（> 30% 缺失直接删除）
    thresh = int(len(merged) * 0.7)
    merged = merged.dropna(axis=1, thresh=thresh)
    # 行级别只丢弃核心列缺失的行
    core   = ['long_label', 'short_label', '5m_adx', '1h_adx']
    core   = [c for c in core if c in merged.columns]
    merged = merged.dropna(subset=core)
    return merged.reset_index(drop=True)
