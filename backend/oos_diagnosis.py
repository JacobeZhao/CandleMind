"""
OOS 止损率诊断脚本 — 深度分析 BTC/BNB 2025年真实OOS表现
"""
import sys, os, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, r'E:\File\Projects\CandleMind\backend')
os.chdir(r'E:\File\Projects\CandleMind\backend')

import pandas as pd
import numpy as np
from app.services.ml_strategy import MLTrendParams, load_scored_bars, simulate_ml_trend, calc_metrics

SYMBOLS = ['BTCUSDT', 'BNBUSDT']
OOS_START = '2025-01-01'
OOS_END   = '2026-06-13'
TRAIN_START = '2023-01-01'
TRAIN_END   = '2024-06-30'

# ═══════════════════════════════════════════════
# 1. 加载OOS数据 + 单独加载训练期（不打分，只要ATR/close）
# ═══════════════════════════════════════════════
all_bars = {}    # OOS scored bars
all_train_raw = {}  # 训练期原始bars（无需打分，只需ATR/close）
all_trades = {}
all_params = {}

for sym in SYMBOLS:
    print(f"\n{'='*60}")
    print(f"加载 {sym} OOS数据 + 模型打分...")
    # OOS scored bars
    oos_bars = load_scored_bars(sym, start=OOS_START, end=OOS_END)
    oos_bars['dt'] = pd.to_datetime(oos_bars['open_time'].astype(np.int64), unit='ms')
    all_bars[sym] = oos_bars

    # 训练期原始labels（只需OHLCV+ATR，不打分）
    from pathlib import Path
    MARKET_ROOT = Path(r'G:\5、金融交易')
    label_path = MARKET_ROOT / 'labels' / f'{sym}_5m_labels.parquet'
    train_raw = pd.read_parquet(label_path)
    if train_raw['open_time'].dtype.kind == 'M':
        train_raw['open_time'] = (train_raw['open_time'].astype(np.int64) // 1_000_000).astype(np.int64)
    train_raw['dt'] = pd.to_datetime(train_raw['open_time'].astype(np.int64), unit='ms')
    all_train_raw[sym] = train_raw[(train_raw['dt'] >= TRAIN_START) & (train_raw['dt'] < TRAIN_END)].copy()

    # OOS模拟
    params = MLTrendParams.from_thresholds(sym)
    all_params[sym] = params
    trades = simulate_ml_trend(oos_bars, params, symbol=sym)
    all_trades[sym] = trades

    m = calc_metrics(trades)
    print(f"\n{sym} OOS回测结果:")
    print(f"  总交易: {m['n_trades']}  做多: {m['long_trades']}  做空: {m['short_trades']}")
    print(f"  胜率: {m['win_rate']:.1%}  平均R: {m['avg_r']:.3f}R  总R: {m['total_r']:.2f}R")
    print(f"  最大回撤: {m['max_dd_r']:.2f}R  Sharpe: {m['sharpe']:.3f}")
    by_r = m.get('by_reason', {})
    for reason, stats in by_r.items():
        label = {'stop': '止损', 'ml_exit': 'ML早退', 'ml_reversal': 'ML反向', 'end': '收盘'}.get(reason, reason)
        print(f"  [{label}] n={stats['n']}  胜率={stats['win_rate']:.1%}  avgR={stats['avg_r']:.3f}  totalR={stats['total_r']:.2f}")

# ═══════════════════════════════════════════════
# 2. ATR Regime 对比: 训练期 vs OOS
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("2. ATR Regime 对比 (训练期 vs 验证期 vs OOS)")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_bars = all_bars[sym]   # already OOS only (2025-01-01+)
    train_bars = all_train_raw[sym]  # raw labels, TRAIN_START~TRAIN_END

    # 验证期: load from labels parquet (no scoring needed, just ATR/close)
    MARKET_ROOT_P = Path(r'G:\5、金融交易')
    label_path = MARKET_ROOT_P / 'labels' / f'{sym}_5m_labels.parquet'
    val_raw = pd.read_parquet(label_path)
    if val_raw['open_time'].dtype.kind == 'M':
        val_raw['open_time'] = (val_raw['open_time'].astype(np.int64) // 1_000_000).astype(np.int64)
    val_raw['dt'] = pd.to_datetime(val_raw['open_time'].astype(np.int64), unit='ms')
    val_bars = val_raw[(val_raw['dt'] >= '2024-07-01') & (val_raw['dt'] < '2025-01-01')].copy()

    print(f"\n{sym} ATR/Close (相对ATR %):")
    for name, subset in [('训练期 2023~2024H1', train_bars),
                          ('验证期 2024H2',      val_bars),
                          ('OOS  2025~2026',    oos_bars)]:
        if 'atr' not in subset.columns or 'close' not in subset.columns:
            print(f"  {name}: 缺少atr/close列")
            continue
        atr_pct = (subset['atr'] / subset['close']).replace([np.inf,-np.inf], np.nan).dropna() * 100
        print(f"  {name}: mean={atr_pct.mean():.3f}%  med={atr_pct.median():.3f}%  "
              f"p75={atr_pct.quantile(.75):.3f}%  p95={atr_pct.quantile(.95):.3f}%  "
              f"p99={atr_pct.quantile(.99):.3f}%")

    # 初始止损在训练期 vs OOS 的绝对大小对比
    p = all_params[sym]
    print(f"  initial_stop_mult={p.initial_stop_mult}x  atr_trail={p.atr_trail}x")
    for name, subset in [('训练期', train_bars), ('OOS', oos_bars)]:
        if 'atr' not in subset.columns or 'close' not in subset.columns:
            continue
        atr_pct = (subset['atr'] / subset['close']).replace([np.inf,-np.inf], np.nan).dropna() * 100
        stop_pct = atr_pct * p.initial_stop_mult
        print(f"  {name} 初始止损距离: mean={stop_pct.mean():.3f}%  med={stop_pct.median():.3f}%  "
              f"p95={stop_pct.quantile(.95):.3f}%")

# ═══════════════════════════════════════════════
# 3. 止损交易 vs 盈利交易 特征深度对比
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("3. 止损交易 vs 盈利交易 特征对比")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_bars = all_bars[sym].reset_index(drop=True)   # already OOS
    trades = all_trades[sym]

    if not trades:
        print(f"  {sym}: 无交易")
        continue

    stop_trades = [t for t in trades if t.reason == 'stop']
    win_trades  = [t for t in trades if t.pnl_r > 0]
    loss_trades = [t for t in trades if t.pnl_r < 0]

    print(f"\n{sym}  止损={len(stop_trades)}  盈利={len(win_trades)}  亏损={len(loss_trades)}")

    # 入场概率对比
    print(f"\n  入场 entry_prob 对比:")
    for label, group in [('止损', stop_trades), ('盈利', win_trades)]:
        if group:
            ep = [t.entry_prob for t in group]
            print(f"    {label}: mean={np.mean(ep):.4f}  med={np.median(ep):.4f}  "
                  f"min={np.min(ep):.4f}  max={np.max(ep):.4f}")

    # 建立 ts→行 快速查找
    ts_index = {int(row['open_time']): i for i, row in oos_bars.iterrows()}

    # 特征对比
    feat_cols = {
        'vol_regime':   '5m_vol_regime',
        'hurst':        '5m_hurst',
        'ema_align':    '5m_ema_align_score',
        'long_prob':    'long_prob',
        'short_prob':   'short_prob',
    }
    available = {k: v for k, v in feat_cols.items() if v in oos_bars.columns}

    print(f"\n  入场时特征均值对比:")
    header = f"    {'特征':<18}" + f"{'止损':>10}" + f"{'盈利':>10}" + f"{'差异':>10}"
    print(header)

    for label, col in available.items():
        stop_vals, win_vals = [], []
        for t in stop_trades:
            idx = ts_index.get(int(t.entry_time))
            if idx is not None and idx < len(oos_bars):
                v = oos_bars.at[idx, col]
                if not np.isnan(v):
                    stop_vals.append(v)
        for t in win_trades:
            idx = ts_index.get(int(t.entry_time))
            if idx is not None and idx < len(oos_bars):
                v = oos_bars.at[idx, col]
                if not np.isnan(v):
                    win_vals.append(v)
        s_mean = np.mean(stop_vals) if stop_vals else float('nan')
        w_mean = np.mean(win_vals) if win_vals else float('nan')
        diff = w_mean - s_mean if (stop_vals and win_vals) else float('nan')
        print(f"    {label:<18}{s_mean:>10.4f}{w_mean:>10.4f}{diff:>+10.4f}")

    # 持仓时长分布
    if stop_trades:
        durs = [t.duration_bars for t in stop_trades]
        print(f"\n  止损交易持仓时长(bars):")
        print(f"    mean={np.mean(durs):.1f}  med={np.median(durs):.0f}  "
              f"p25={np.percentile(durs,25):.0f}  p75={np.percentile(durs,75):.0f}  "
              f"max={np.max(durs):.0f}")
        very_short = sum(1 for d in durs if d <= 3)
        short_10   = sum(1 for d in durs if d <= 10)
        print(f"    ≤3bars即止损: {very_short}/{len(durs)} ({very_short/len(durs):.1%})")
        print(f"    ≤10bars即止损: {short_10}/{len(durs)} ({short_10/len(durs):.1%})")

    # 退出概率对比 (what prob level at exit)
    print(f"\n  出场时 exit_prob (same_dir) 对比:")
    for label, group in [('止损', stop_trades), ('盈利', win_trades)]:
        if group:
            ep = [t.exit_prob for t in group]
            print(f"    {label}: mean={np.mean(ep):.4f}  med={np.median(ep):.4f}  "
                  f"p25={np.percentile(ep,25):.4f}  p75={np.percentile(ep,75):.4f}")

# ═══════════════════════════════════════════════
# 4. 入场方向 vs 月度市场方向 (趋势吻合度)
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("4. 入场方向 vs 月度市场方向")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_bars = all_bars[sym].copy()   # already OOS-only
    trades = all_trades[sym]

    if not trades:
        continue

    oos_bars['month'] = oos_bars['dt'].dt.to_period('M')
    monthly_ret = oos_bars.groupby('month').apply(
        lambda x: (x['close'].iloc[-1] - x['close'].iloc[0]) / x['close'].iloc[0]
    )

    # 计算每笔交易入场方向是否与当月市场方向一致
    aligned, anti = 0, 0
    monthly_stats = {}
    for t in trades:
        dt = pd.Timestamp(int(t.entry_time), unit='ms')
        m  = dt.to_period('M')
        mkt_ret = monthly_ret.get(m, None)
        if mkt_ret is None:
            continue
        mkt_dir = 1 if mkt_ret > 0 else -1
        if t.direction == mkt_dir:
            aligned += 1
        else:
            anti += 1
        k = str(m)
        if k not in monthly_stats:
            monthly_stats[k] = {'long': 0, 'short': 0, 'mkt_ret': mkt_ret}
        if t.direction == 1:
            monthly_stats[k]['long'] += 1
        else:
            monthly_stats[k]['short'] += 1

    total = aligned + anti
    print(f"\n{sym}: 入场方向与月度市场方向吻合率 {aligned}/{total} = {aligned/total:.1%}")
    print(f"  (顺势={aligned}  逆势={anti})")

    print(f"\n  月度交易方向分布:")
    print(f"  {'月份':<10} {'多头':>6} {'空头':>6} {'月度涨跌':>10} {'主导':<6}")
    for month, stats in sorted(monthly_stats.items()):
        ret_pct = stats['mkt_ret'] * 100
        dominant = '上涨' if stats['mkt_ret'] > 0 else '下跌'
        print(f"  {month:<10} {stats['long']:>6} {stats['short']:>6} {ret_pct:>+9.1f}% {dominant:<6}")

# ═══════════════════════════════════════════════
# 5. 关键 gate 拦截率分析（vol_gate/hurst_gate/ema_align_gate）
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("5. 入场Gate过滤率分析（哪些Gate拦截了多少潜在信号）")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_bars = all_bars[sym].reset_index(drop=True)   # already OOS-only
    params = all_params[sym]
    p = params

    lp = oos_bars['long_prob'].values
    sp = oos_bars['short_prob'].values

    vol_regime_arr = oos_bars['5m_vol_regime'].values  if '5m_vol_regime' in oos_bars.columns else None
    ema_align_arr  = oos_bars['5m_ema_align_score'].values if '5m_ema_align_score' in oos_bars.columns else None
    hurst_arr      = oos_bars['5m_hurst'].values if '5m_hurst' in oos_bars.columns else None

    gap_req = p.min_prob_gap_large_cap

    raw_long  = (lp >= p.entry_long_threshold)  & ((lp - sp) >= gap_req)
    raw_short = (sp >= p.entry_short_threshold) & ((sp - lp) >= gap_req)
    raw_any   = raw_long | raw_short

    n_raw = int(raw_any.sum())

    # Vol gate
    if vol_regime_arr is not None:
        blocked_vol = raw_any & (vol_regime_arr >= 2.0)
        after_vol_long  = raw_long  & (vol_regime_arr < 2.0)
        after_vol_short = raw_short & (vol_regime_arr < 2.0)
    else:
        blocked_vol = np.zeros(len(oos_bars), dtype=bool)
        after_vol_long, after_vol_short = raw_long, raw_short

    # EMA gate
    if ema_align_arr is not None:
        blocked_ema_long  = after_vol_long  & (ema_align_arr < 1.0)
        blocked_ema_short = after_vol_short & (ema_align_arr > -1.0)
        after_ema_long  = after_vol_long  & (ema_align_arr >= 1.0)
        after_ema_short = after_vol_short & (ema_align_arr <= -1.0)
    else:
        blocked_ema_long = blocked_ema_short = np.zeros(len(oos_bars), dtype=bool)
        after_ema_long, after_ema_short = after_vol_long, after_vol_short

    # Hurst gate
    if hurst_arr is not None:
        blocked_hurst_long  = after_ema_long  & (hurst_arr < p.hurst_entry_min)
        blocked_hurst_short = after_ema_short & (hurst_arr < p.hurst_entry_min)
        final_long  = after_ema_long  & (hurst_arr >= p.hurst_entry_min)
        final_short = after_ema_short & (hurst_arr >= p.hurst_entry_min)
    else:
        blocked_hurst_long = blocked_hurst_short = np.zeros(len(oos_bars), dtype=bool)
        final_long, final_short = after_ema_long, after_ema_short

    n_final = int((final_long | final_short).sum())

    print(f"\n{sym} 原始信号→过滤:")
    print(f"  总bars: {len(oos_bars):,}  原始触发(含gap): {n_raw} ({n_raw/len(oos_bars):.2%})")
    print(f"  vol_gate拦截: {int(blocked_vol.sum())} ({int(blocked_vol.sum())/max(n_raw,1):.1%} of raw)")
    if ema_align_arr is not None:
        bl = int(blocked_ema_long.sum()) + int(blocked_ema_short.sum())
        print(f"  ema_align_gate拦截: {bl} ({bl/max(n_raw,1):.1%} of raw)")
    if hurst_arr is not None:
        bh = int(blocked_hurst_long.sum()) + int(blocked_hurst_short.sum())
        print(f"  hurst_gate拦截: {bh} ({bh/max(n_raw,1):.1%} of raw)")
    print(f"  最终通过gate: {n_final} ({n_final/max(n_raw,1):.1%} of raw)")

    # 方向偏向
    print(f"  最终做多触发: {int(final_long.sum())}  做空触发: {int(final_short.sum())}")

    # EMA align 分布
    if ema_align_arr is not None:
        vals = ema_align_arr
        print(f"  EMA align score分布: "
              f"<=-2:{(vals<=-2).mean():.1%}  -1:{((vals>-2)&(vals<=-1)).mean():.1%}  "
              f"0:{((vals>-1)&(vals<1)).mean():.1%}  "
              f"1:{((vals>=1)&(vals<2)).mean():.1%}  >=2:{(vals>=2).mean():.1%}")

    # Hurst分布
    if hurst_arr is not None:
        h = hurst_arr[~np.isnan(hurst_arr)]
        print(f"  Hurst分布: <0.45:{(h<0.45).mean():.1%}  0.45-0.50:{((h>=0.45)&(h<0.5)).mean():.1%}  "
              f"0.50-0.55:{((h>=0.5)&(h<0.55)).mean():.1%}  >0.55:{(h>=0.55).mean():.1%}")

# ═══════════════════════════════════════════════
# 6. 月度P&L分解 (找出最坏月份的结构)
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("6. 月度P&L分解")
print(f"{'='*60}")

for sym in SYMBOLS:
    trades = all_trades[sym]
    if not trades:
        continue

    rows = []
    for t in trades:
        dt = pd.Timestamp(int(t.exit_time), unit='ms')
        rows.append({
            'month': dt.to_period('M'),
            'pnl_r': t.pnl_r,
            'reason': t.reason,
            'direction': 'long' if t.direction == 1 else 'short',
            'duration_bars': t.duration_bars,
        })
    df = pd.DataFrame(rows)

    monthly = df.groupby('month').agg(
        n=('pnl_r','count'),
        total_r=('pnl_r','sum'),
        win_rate=('pnl_r', lambda x: (x>0).mean()),
        avg_dur=('duration_bars','mean'),
        n_stop=('reason', lambda x: (x=='stop').sum()),
    ).reset_index()
    monthly['stop_rate'] = monthly['n_stop'] / monthly['n']

    print(f"\n{sym} 月度P&L:")
    print(f"  {'月份':<10} {'交易数':>6} {'总R':>8} {'胜率':>7} {'止损率':>7} {'均持仓(b)':>10}")
    for _, row in monthly.iterrows():
        print(f"  {str(row['month']):<10} {row['n']:>6} {row['total_r']:>+8.2f} "
              f"{row['win_rate']:>6.1%} {row['stop_rate']:>6.1%} {row['avg_dur']:>10.1f}")

# ═══════════════════════════════════════════════
# 7. 止损充足性: 初始止损距离 vs 实际不利偏移 估算
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("7. 止损充足性分析 (初始止损距离占入场价%)")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_bars = all_bars[sym].reset_index(drop=True)   # already OOS-only
    trades = all_trades[sym]
    stop_trades = [t for t in trades if t.reason == 'stop']

    if not stop_trades:
        continue

    # 初始止损距离（绝对）和 %
    init_stop_dists = []
    for t in stop_trades:
        dist = abs(t.entry_price - t.initial_stop)
        dist_pct = dist / t.entry_price * 100
        init_stop_dists.append(dist_pct)

    print(f"\n{sym} 止损交易 初始止损距离 (%):")
    print(f"  mean={np.mean(init_stop_dists):.3f}%  med={np.median(init_stop_dists):.3f}%  "
          f"p25={np.percentile(init_stop_dists,25):.3f}%  p75={np.percentile(init_stop_dists,75):.3f}%  "
          f"p95={np.percentile(init_stop_dists,95):.3f}%")

    # 出场价 vs 入场价 亏损幅度（应与止损距离相近，看是否trailing stop更新过）
    actual_losses = []
    for t in stop_trades:
        loss_pct = abs(t.exit_price - t.entry_price) / t.entry_price * 100
        actual_losses.append(loss_pct)

    print(f"  实际出场亏损幅度 (%):")
    print(f"  mean={np.mean(actual_losses):.3f}%  med={np.median(actual_losses):.3f}%  "
          f"p25={np.percentile(actual_losses,25):.3f}%  p75={np.percentile(actual_losses,75):.3f}%  "
          f"p95={np.percentile(actual_losses,95):.3f}%")

    # 是否trailing stop触发(出场价 < 初始止损说明trailing有获利保护)
    trail_wins = sum(1 for t in stop_trades
                     if (t.direction == 1 and t.exit_price > t.initial_stop) or
                        (t.direction == -1 and t.exit_price < t.initial_stop))
    print(f"  trailing止损（有获利保护）触发: {trail_wins}/{len(stop_trades)} ({trail_wins/len(stop_trades):.1%})")

    # pnl_r 分布（止损后实际R）
    stop_pnls = [t.pnl_r for t in stop_trades]
    print(f"  止损后pnl_r分布: mean={np.mean(stop_pnls):.3f}  med={np.median(stop_pnls):.3f}  "
          f"p5={np.percentile(stop_pnls,5):.3f}  p25={np.percentile(stop_pnls,25):.3f}  "
          f"min={np.min(stop_pnls):.3f}")

# ═══════════════════════════════════════════════
# 8. 模型区分度：OOS概率分布
# ═══════════════════════════════════════════════
print(f"\n{'='*60}")
print("8. OOS模型概率分布（信号强度是否退化）")
print(f"{'='*60}")

for sym in SYMBOLS:
    oos_b = all_bars[sym]   # OOS only; training/val not scored, skip prob dist for those

    print(f"\n{sym} long_prob 分布 (OOS only — training period not scored in this run):")
    lp = oos_b['long_prob'].dropna()
    print(f"  OOS: mean={lp.mean():.4f}  std={lp.std():.4f}  "
          f"p5={lp.quantile(.05):.4f}  p25={lp.quantile(.25):.4f}  "
          f"p75={lp.quantile(.75):.4f}  p95={lp.quantile(.95):.4f}  "
          f">0.55:{(lp>0.55).mean():.2%}  >0.58:{(lp>0.58).mean():.2%}  >0.60:{(lp>0.60).mean():.2%}")

    sp = oos_b['short_prob'].dropna()
    print(f"  OOS short_prob: mean={sp.mean():.4f}  std={sp.std():.4f}  "
          f">0.55:{(sp>0.55).mean():.2%}  >0.58:{(sp>0.58).mean():.2%}  >0.60:{(sp>0.60).mean():.2%}")

    # Monthly signal density
    oos_b2 = oos_b.copy()
    oos_b2['month'] = oos_b2['dt'].dt.to_period('M')
    p_obj = all_params[sym]
    oos_b2['any_signal'] = ((oos_b2['long_prob'] >= p_obj.entry_long_threshold) |
                             (oos_b2['short_prob'] >= p_obj.entry_short_threshold))
    monthly_sig = oos_b2.groupby('month').agg(
        bars=('long_prob','count'),
        sig=('any_signal','sum'),
        avg_lp=('long_prob','mean'),
        avg_sp=('short_prob','mean'),
    )
    monthly_sig['sig_rate'] = monthly_sig['sig'] / monthly_sig['bars']
    print(f"  月度信号密度 (prob超阈值前gate拦截):")
    for m, row in monthly_sig.iterrows():
        print(f"    {m}: sig_rate={row['sig_rate']:.2%}  avg_long={row['avg_lp']:.4f}  avg_short={row['avg_sp']:.4f}")

print(f"\n{'='*60}")
print("诊断完成")
print(f"{'='*60}")
