import sys, os, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, r'E:\File\Projects\CandleMind\backend')
import pandas as pd
import numpy as np

sym = 'BTCUSDT'
label_path = rf'G:\5、金融交易\labels\{sym}_5m_labels.parquet'
df = pd.read_parquet(label_path)

if 'open_time' in df.columns:
    if df['open_time'].dtype.kind != 'M':
        df['dt'] = pd.to_datetime(df['open_time'], unit='ms')
    else:
        df['dt'] = df['open_time']
else:
    df['dt'] = df.index

df['month'] = df['dt'].dt.to_period('M')
train = df[(df['dt'] >= '2023-01-01') & (df['dt'] < '2024-07-01')]
val   = df[(df['dt'] >= '2024-07-01') & (df['dt'] < '2025-01-01')]
oos   = df[df['dt'] >= '2025-01-01']

print(f"columns: {list(df.columns[:30])}")
print(f"total rows: {len(df):,}")
print(f"time range: {df['dt'].min()} ~ {df['dt'].max()}")
print(f"\ntrain: {len(train):,}  val: {len(val):,}  OOS: {len(oos):,}")

for col in ['long_label', 'short_label']:
    if col not in df.columns:
        continue
    print(f"\n=== {col} ===")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        rate = subset[col].mean()
        print(f"  {name}: positive_rate={rate:.3f}  n={len(subset):,}")

    monthly = df.groupby('month')[col].mean()
    print(f"  monthly positive rates:")
    for m, r in monthly.items():
        print(f"    {m}: {r:.3f}")

# Consecutive run length distribution (trend persistence)
print("\n=== Consecutive positive run lengths ===")
for col in ['long_label', 'short_label']:
    if col not in df.columns:
        continue
    for name, subset in [('train', train), ('OOS', oos)]:
        s = subset[col].values
        runs = []
        cur = 0
        for v in s:
            if v == 1:
                cur += 1
            else:
                if cur > 0:
                    runs.append(cur)
                cur = 0
        if cur > 0:
            runs.append(cur)
        if runs:
            runs = np.array(runs)
            print(f"  {col} {name}: mean={runs.mean():.2f} median={np.median(runs):.1f} max={runs.max()} p75={np.percentile(runs,75):.1f} p90={np.percentile(runs,90):.1f}")

# Correlation and mutual exclusivity
if 'long_label' in df.columns and 'short_label' in df.columns:
    corr_train = train['long_label'].corr(train['short_label'])
    corr_val   = val['long_label'].corr(val['short_label'])
    corr_oos   = oos['long_label'].corr(oos['short_label'])
    print(f"\nlong/short correlation: train={corr_train:.4f}  val={corr_val:.4f}  OOS={corr_oos:.4f}")

    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        both = subset[(subset['long_label']==1) & (subset['short_label']==1)]
        none = subset[(subset['long_label']==0) & (subset['short_label']==0)]
        print(f"  {name}: both=1: {len(both)/len(subset):.4f}  both=0: {len(none)/len(subset):.4f}")

# Regime column
if 'regime' in df.columns:
    print("\n=== regime distribution ===")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        print(f"  {name}:")
        print(subset['regime'].value_counts(normalize=True).to_string())
else:
    print("\nNo 'regime' column found.")

# Check all columns
print(f"\nAll columns: {list(df.columns)}")
print(f"\nDtypes:\n{df.dtypes}")
print(f"\nSample data (first 3 rows):\n{df.head(3).to_string()}")
