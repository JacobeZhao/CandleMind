import sys, os, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, r'E:\File\Projects\CandleMind\backend')
import pandas as pd
import numpy as np

sym = 'BTCUSDT'
label_path = rf'G:\5、金融交易\labels\{sym}_5m_labels.parquet'
df = pd.read_parquet(label_path)

if df['open_time'].dtype.kind != 'M':
    df['dt'] = pd.to_datetime(df['open_time'], unit='ms')
else:
    df['dt'] = df['open_time']

df['month'] = df['dt'].dt.to_period('M')
train = df[(df['dt'] >= '2023-01-01') & (df['dt'] < '2024-07-01')]
val   = df[(df['dt'] >= '2024-07-01') & (df['dt'] < '2025-01-01')]
oos   = df[df['dt'] >= '2025-01-01']

# -------------------------------------------
# 1. long_barrier_hit / short_barrier_hit breakdown
# -------------------------------------------
print("=== Barrier Hit Distribution ===")
for col in ['long_barrier_hit', 'short_barrier_hit']:
    print(f"\n{col}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        vc = subset[col].value_counts(normalize=True)
        print(f"  {name}: {vc.to_dict()}")

# -------------------------------------------
# 2. Profit_R stats for winning trades
# -------------------------------------------
print("\n=== Profit R stats (winning trades) ===")
for col, lbl in [('long_profit_r', 'long_label'), ('short_profit_r', 'short_label')]:
    print(f"\n{col}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        wins = subset[subset[lbl] == 1][col]
        losses = subset[subset[lbl] == 0][col]
        print(f"  {name} wins (label=1): mean={wins.mean():.3f} std={wins.std():.3f} n={len(wins):,}")
        print(f"  {name} loss (label=0): mean={losses.mean():.3f} std={losses.std():.3f} n={len(losses):,}")

# -------------------------------------------
# 3. Duration distribution by label
# -------------------------------------------
print("\n=== Duration Bars (positive label only) ===")
for col, lbl in [('long_duration_bars', 'long_label'), ('short_duration_bars', 'short_label')]:
    print(f"\n{col}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        pos = subset[subset[lbl] == 1][col]
        print(f"  {name}: mean={pos.mean():.1f} median={pos.median():.1f} p75={pos.quantile(0.75):.1f} p90={pos.quantile(0.90):.1f}")

# -------------------------------------------
# 4. Trend Sharpe and Consistency
# -------------------------------------------
print("\n=== Trend Quality (positive label only) ===")
for direction in ['long', 'short']:
    lbl = f'{direction}_label'
    sharpe_col = f'{direction}_trend_sharpe'
    consist_col = f'{direction}_trend_consistency'
    print(f"\n{direction}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        pos = subset[subset[lbl] == 1]
        print(f"  {name}: sharpe={pos[sharpe_col].mean():.3f}  consistency={pos[consist_col].mean():.3f}")

# -------------------------------------------
# 5. Max adverse excursion (how much it went against before hitting TP)
# -------------------------------------------
print("\n=== Max Adverse R (positive label = TP hit) ===")
for direction in ['long', 'short']:
    lbl = f'{direction}_label'
    mae_col = f'{direction}_max_adverse_r'
    print(f"\n{direction}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        pos = subset[subset[lbl] == 1][mae_col]
        print(f"  {name}: mean_adverse={pos.mean():.3f} p75={pos.quantile(0.75):.3f} p90={pos.quantile(0.90):.3f}")

# -------------------------------------------
# 6. regime_profit_mult distribution
# -------------------------------------------
print("\n=== regime_profit_mult distribution ===")
for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
    rpm = subset['regime_profit_mult']
    print(f"  {name}: mean={rpm.mean():.3f} median={rpm.median():.3f} std={rpm.std():.3f}")
    vc = rpm.value_counts(normalize=True).head(5)
    print(f"    top values: {vc.to_dict()}")

# -------------------------------------------
# 7. best_direction in OOS vs train
# -------------------------------------------
print("\n=== best_direction distribution ===")
for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
    vc = subset['best_direction'].value_counts(normalize=True)
    print(f"  {name}: {vc.to_dict()}")

# -------------------------------------------
# 8. Monthly breakdown of long vs short win rates in OOS
# -------------------------------------------
print("\n=== Monthly OOS: long_label vs short_label positive rate ===")
oos_monthly = oos.groupby('month').agg(
    long_rate=('long_label', 'mean'),
    short_rate=('short_label', 'mean'),
    long_meta=('long_meta_label', 'mean'),
    short_meta=('short_meta_label', 'mean'),
    n=('long_label', 'count')
).reset_index()
print(oos_monthly.to_string(index=False))

# -------------------------------------------
# 9. Meta label rates
# -------------------------------------------
print("\n=== Meta Label Positive Rates ===")
for col in ['long_meta_label', 'short_meta_label']:
    if col not in df.columns:
        continue
    print(f"\n{col}:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        rate = subset[col].mean()
        print(f"  {name}: {rate:.3f}  n={len(subset):,}")
