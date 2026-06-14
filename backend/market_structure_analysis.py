import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np

sym = 'BTCUSDT'
feat_path  = rf'G:\5、金融交易\features_ml\{sym}_features.parquet'
label_path = rf'G:\5、金融交易\labels\{sym}_5m_labels.parquet'

# Load label file only for essential columns
df_lab = pd.read_parquet(label_path, columns=['open_time','close','atr','long_label','short_label'])

# Load only needed feature columns
needed_cols = [
    'open_time',
    '5m_hurst', '1h_hurst', '4h_hurst', '1d_hurst',
    '5m_adx', '1h_adx', '4h_adx',
    '5m_vol_regime', '1h_vol_regime', '4h_vol_regime',
    '5m_atr_pct20', '1h_atr_pct20', '4h_atr_pct20',
    '5m_taker_buy_ratio', '1h_taker_buy_ratio', '4h_taker_buy_ratio',
    '5m_funding_rate', '5m_funding_crowded_long', '5m_funding_crowded_short',
    '5m_ema_align_score', '1h_ema_align_score', '4h_ema_align_score',
    '5m_mfi', '1h_mfi', '4h_mfi',
    '5m_autocorr_1', '1h_autocorr_1', '4h_autocorr_1',
    '5m_trend_r2_20', '1h_trend_r2_20', '4h_trend_r2_20',
    '5m_bb_width_20', '1h_bb_width_20', '4h_bb_width_20',
    '5m_rv24', '1h_rv24', '4h_rv24',
    'ia_adx_mom_1h', 'ia_vol_atr_1h', 'ia_decouple_1h', 'ia_trend_confirm_4h',
]

df_feat = pd.read_parquet(feat_path, columns=needed_cols)

df_feat['dt'] = pd.to_datetime(df_feat['open_time'], unit='ms') if df_feat['open_time'].dtype.kind != 'M' else df_feat['open_time']
df_lab['dt']  = pd.to_datetime(df_lab['open_time'],  unit='ms') if df_lab['open_time'].dtype.kind  != 'M' else df_lab['open_time']

df = df_feat.merge(df_lab[['open_time','close','atr','long_label','short_label']], on='open_time', how='inner')
print(f"Merged rows: {len(df)}, date range: {df['dt'].min()} ~ {df['dt'].max()}")

train = df[(df['dt'] >= '2023-01-01') & (df['dt'] < '2024-07-01')]
val   = df[(df['dt'] >= '2024-07-01') & (df['dt'] < '2025-01-01')]
oos   = df[df['dt'] >= '2025-01-01']

print(f"Train: {len(train)}, Val: {len(val)}, OOS: {len(oos)}")
print(f"OOS date range: {oos['dt'].min()} ~ {oos['dt'].max()}")

def stats(series, name):
    s = series.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) == 0:
        return f"  {name}: NO DATA"
    return (f"  {name}: mean={s.mean():.4f}  med={s.median():.4f}  std={s.std():.4f}  "
            f"p25={s.quantile(.25):.4f}  p75={s.quantile(.75):.4f}  N={len(s)}")

sep = "=" * 70

print("\n" + sep)
print("=== 1. Hurst Exponent (>0.5=trend, <0.5=mean-revert) ===")
print(sep)
for col in ['5m_hurst', '1h_hurst', '4h_hurst', '1d_hurst']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train 2023-2024H1'))
    print(stats(val[col],   'Val   2024H2     '))
    print(stats(oos[col],   'OOS   2025+      '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        pct_trend = (s > 0.5).mean()
        pct_mr    = (s < 0.5).mean()
        print(f"    {nm}: Hurst>0.5(trend)={pct_trend:.1%}  Hurst<0.5(MR)={pct_mr:.1%}")

print("\n" + sep)
print("=== 2. ADX Distribution (>25=strong trend) ===")
print(sep)
for col in ['5m_adx', '1h_adx', '4h_adx']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        print(f"    {nm}: ADX>25={( s>25).mean():.1%}  ADX>35={( s>35).mean():.1%}  ADX<20={(s<20).mean():.1%}")

print("\n" + sep)
print("=== 3. Volatility Structure ===")
print(sep)
for col in ['5m_vol_regime', '1h_vol_regime', '4h_vol_regime']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))

for col in ['5m_atr_pct20', '1h_atr_pct20', '4h_atr_pct20']:
    if col not in df.columns:
        continue
    print(f"\n[{col}] (ATR%)")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))

print("\n" + sep)
print("=== 4. Taker Buy Ratio (>0.5=bull dominance) ===")
print(sep)
for col in ['5m_taker_buy_ratio', '1h_taker_buy_ratio', '4h_taker_buy_ratio']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        print(f"    {nm}: >0.55(bull-dom)={(s>0.55).mean():.1%}  <0.45(bear-dom)={(s<0.45).mean():.1%}")

print("\n" + sep)
print("=== 5. Funding Rate ===")
print(sep)
for col in ['5m_funding_rate', '5m_funding_crowded_long', '5m_funding_crowded_short']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    if col == '5m_funding_rate':
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            pos_pct     = (s > 0).mean()
            crowded_l   = (s > 0.01).mean()
            crowded_s   = (s < -0.005).mean()
            print(f"    {nm}: positive={pos_pct:.1%}  crowded_long(>0.01)={crowded_l:.1%}  crowded_short(<-0.005)={crowded_s:.1%}")

print("\n" + sep)
print("=== 6. EMA Alignment Score ===")
print(sep)
for col in ['5m_ema_align_score', '1h_ema_align_score', '4h_ema_align_score']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        bull_align = (s >= 0.8).mean()
        bear_align = (s <= -0.8).mean()
        neutral    = (s.abs() < 0.3).mean()
        print(f"    {nm}: bull_align(>=0.8)={bull_align:.1%}  bear_align(<=-0.8)={bear_align:.1%}  neutral={neutral:.1%}")

print("\n" + sep)
print("=== 7. Autocorrelation (momentum vs mean-reversion) ===")
print(sep)
for col in ['5m_autocorr_1', '1h_autocorr_1', '4h_autocorr_1']:
    if col not in df.columns:
        continue
    print(f"\n[{col}] (+=momentum, -=mean-revert)")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        mom_pct = (s > 0.05).mean()
        mr_pct  = (s < -0.05).mean()
        print(f"    {nm}: momentum(>0.05)={mom_pct:.1%}  mean-revert(<-0.05)={mr_pct:.1%}")

print("\n" + sep)
print("=== 8. Trend R2 ===")
print(sep)
for col in ['5m_trend_r2_20', '1h_trend_r2_20', '4h_trend_r2_20']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) == 0:
            continue
        high_trend = (s > 0.7).mean()
        print(f"    {nm}: R2>0.7(strong trend)={high_trend:.1%}")

print("\n" + sep)
print("=== 9. Bollinger Band Width (choppy proxy) ===")
print(sep)
for col in ['5m_bb_width_20', '1h_bb_width_20', '4h_bb_width_20']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))

print("\n" + sep)
print("=== 10. Monthly Price Direction (OOS 2025+) ===")
print(sep)
df_oos = oos.copy()
df_oos['month'] = df_oos['dt'].dt.to_period('M')
oos_monthly = df_oos.groupby('month').agg(
    open_p=('close', 'first'),
    close_p=('close', 'last'),
    atr_mean=('atr', 'mean'),
    long_lbl_rate=('long_label', 'mean'),
    short_lbl_rate=('short_label', 'mean'),
).reset_index()
oos_monthly['ret'] = (oos_monthly['close_p'] - oos_monthly['open_p']) / oos_monthly['open_p']

def direction(x):
    if x > 0.02:
        return 'UP'
    elif x < -0.02:
        return 'DN'
    return 'FLAT'

oos_monthly['dir'] = oos_monthly['ret'].apply(direction)
print(oos_monthly[['month', 'ret', 'dir', 'atr_mean', 'long_lbl_rate', 'short_lbl_rate']].to_string(index=False))

print("\n--- Monthly Price Direction (Train 2023-2024H1) ---")
df_train = train.copy()
df_train['month'] = df_train['dt'].dt.to_period('M')
train_monthly = df_train.groupby('month').agg(
    open_p=('close', 'first'),
    close_p=('close', 'last'),
    atr_mean=('atr', 'mean'),
    long_lbl_rate=('long_label', 'mean'),
    short_lbl_rate=('short_label', 'mean'),
).reset_index()
train_monthly['ret'] = (train_monthly['close_p'] - train_monthly['open_p']) / train_monthly['open_p']
train_monthly['dir'] = train_monthly['ret'].apply(direction)
print(train_monthly[['month', 'ret', 'dir', 'atr_mean', 'long_lbl_rate', 'short_lbl_rate']].to_string(index=False))

print("\n--- Monthly Summary Stats ---")
for nm, subset_m in [('Train', train_monthly), ('OOS', oos_monthly)]:
    up   = (subset_m['dir'] == 'UP').sum()
    dn   = (subset_m['dir'] == 'DN').sum()
    flat = (subset_m['dir'] == 'FLAT').sum()
    total = len(subset_m)
    print(f"  {nm}: UP={up}/{total}({up/total:.0%})  DN={dn}/{total}({dn/total:.0%})  FLAT={flat}/{total}({flat/total:.0%})")

print("\n" + sep)
print("=== 11. Label Streak (trend persistence) ===")
print(sep)
for col in ['long_label', 'short_label']:
    if col not in df.columns:
        continue
    for nm, subset in [('Train', train), ('Val', val), ('OOS', oos)]:
        s = subset[col].values
        runs = []
        cnt = 0
        for v in s:
            if v == 1:
                cnt += 1
            else:
                if cnt > 0:
                    runs.append(cnt)
                cnt = 0
        if cnt > 0:
            runs.append(cnt)
        runs = np.array(runs)
        if len(runs) == 0:
            continue
        total_active = (s == 1).sum()
        print(f"  {col}/{nm}: mean_run={runs.mean():.1f}bar  med={np.median(runs):.0f}  "
              f"p90={np.percentile(runs, 90):.0f}  n_segments={len(runs)}  active_rate={total_active/len(s):.1%}")

print("\n" + sep)
print("=== 12. Interaction Features ===")
print(sep)
for col in ['ia_adx_mom_1h', 'ia_vol_atr_1h', 'ia_decouple_1h', 'ia_trend_confirm_4h']:
    if col not in df.columns:
        continue
    print(f"\n[{col}]")
    print(stats(train[col], 'Train'))
    print(stats(val[col],   'Val  '))
    print(stats(oos[col],   'OOS  '))

print("\n" + sep)
print("=== 13. Label Rates (signal generation frequency) ===")
print(sep)
for nm, subset in [('Train', train), ('Val', val), ('OOS', oos)]:
    ll = subset['long_label'].mean()
    sl = subset['short_label'].mean()
    print(f"  {nm}: long_label_rate={ll:.2%}  short_label_rate={sl:.2%}")

print("\nDone.")
