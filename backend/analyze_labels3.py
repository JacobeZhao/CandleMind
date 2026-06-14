"""
Check label stationarity vs. the actual market regime in OOS.
Focus: is the LABELING system still generating valid signals in 2025?
Answer: compare label-implied edge vs realized edge.
"""
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
# 1. CORE QUESTION: If you ALWAYS enter long at every 5m bar,
#    what fraction of time does TP fire vs SL fire?
#    This is the label's implied base-rate edge.
# -------------------------------------------
print("=== BASE RATE EDGE (what labels imply) ===")
print("If a perfect oracle filtered to ONLY positive labels:")
for direction in ['long', 'short']:
    lbl = f'{direction}_label'
    profit_col = f'{direction}_profit_r'
    print(f"\n{direction} oracle strategy:")
    for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
        pos = subset[subset[lbl] == 1]
        neg = subset[subset[lbl] == 0]
        # Expected R per trade if you take all positive-labeled bars
        avg_r_pos = pos[profit_col].mean()
        avg_r_neg = neg[profit_col].mean()
        n_pos = len(pos)
        n_neg = len(neg)
        # Overall expected R if model is AUC=0.55 (50% true pos, 50% noise)
        print(f"  {name}: E[R|label=1]={avg_r_pos:.3f}  E[R|label=0]={avg_r_neg:.3f}  ratio={n_pos/(n_pos+n_neg):.3f}")

# -------------------------------------------
# 2. Monthly long vs short ASYMMETRY in OOS
#    (does short dominate in particular months?)
# -------------------------------------------
print("\n=== Monthly short_label - long_label rate (positive = short dominates) ===")
oos_m = oos.groupby('month').agg(
    long_rate=('long_label', 'mean'),
    short_rate=('short_label', 'mean'),
    long_meta=('long_meta_label', 'mean'),
    short_meta=('short_meta_label', 'mean'),
    long_profit_r=('long_profit_r', 'mean'),
    short_profit_r=('short_profit_r', 'mean'),
    close_last=('close', 'last'),
    n=('long_label', 'count')
).reset_index()
oos_m['short_bias'] = oos_m['short_rate'] - oos_m['long_rate']
oos_m['meta_short_bias'] = oos_m['short_meta'] - oos_m['long_meta']
print(oos_m[['month', 'long_rate', 'short_rate', 'short_bias', 'long_meta', 'short_meta', 'meta_short_bias', 'long_profit_r', 'short_profit_r', 'close_last']].to_string(index=False))

# -------------------------------------------
# 3. BNB-specific concern: Let's also check BNB labels
# -------------------------------------------
print("\n\n=== BNB Label Analysis ===")
bnb_path = r'G:\5、金融交易\labels\BNBUSDT_5m_labels.parquet'
try:
    bnb = pd.read_parquet(bnb_path)
    if bnb['open_time'].dtype.kind != 'M':
        bnb['dt'] = pd.to_datetime(bnb['open_time'], unit='ms')
    else:
        bnb['dt'] = bnb['open_time']
    bnb['month'] = bnb['dt'].dt.to_period('M')
    bnb_train = bnb[(bnb['dt'] >= '2023-01-01') & (bnb['dt'] < '2024-07-01')]
    bnb_oos = bnb[bnb['dt'] >= '2025-01-01']

    print("BNB label rates:")
    for name, subset in [('train', bnb_train), ('OOS', bnb_oos)]:
        print(f"  {name}: long={subset['long_label'].mean():.3f} short={subset['short_label'].mean():.3f} n={len(subset):,}")

    print("\nBNB monthly OOS (2025):")
    bnb_oos_m = bnb_oos.groupby('month').agg(
        long_rate=('long_label', 'mean'),
        short_rate=('short_label', 'mean'),
        long_meta=('long_meta_label', 'mean'),
        short_meta=('short_meta_label', 'mean'),
        close_last=('close', 'last'),
        n=('long_label', 'count')
    ).reset_index()
    print(bnb_oos_m.to_string(index=False))

    # BNB July 2025 deep dive
    bnb_jul = bnb[(bnb['dt'] >= '2025-07-01') & (bnb['dt'] < '2025-08-01')]
    print(f"\nBNB July 2025:")
    print(f"  long_label={bnb_jul['long_label'].mean():.3f} short_label={bnb_jul['short_label'].mean():.3f}")
    print(f"  price range: {bnb_jul['close'].min():.1f} ~ {bnb_jul['close'].max():.1f}")
    print(f"  long_profit_r mean: {bnb_jul['long_profit_r'].mean():.3f}")
    print(f"  short_profit_r mean: {bnb_jul['short_profit_r'].mean():.3f}")
except Exception as e:
    print(f"BNB analysis failed: {e}")

# -------------------------------------------
# 4. Statistical stationarity test on label rates
# -------------------------------------------
print("\n=== Stationarity: Monthly std of label rates ===")
for col in ['long_label', 'short_label']:
    monthly = df.groupby('month')[col].mean()
    train_monthly = monthly[monthly.index < pd.Period('2025-01', 'M')]
    oos_monthly = monthly[(monthly.index >= pd.Period('2025-01', 'M')) & (monthly.index < pd.Period('2026-06', 'M'))]
    print(f"\n{col}:")
    print(f"  train: mean={train_monthly.mean():.4f} std={train_monthly.std():.4f}")
    print(f"  OOS  : mean={oos_monthly.mean():.4f} std={oos_monthly.std():.4f}")
    # Are OOS values within 2 sigma of training distribution?
    train_mean = train_monthly.mean()
    train_std = train_monthly.std()
    outliers = oos_monthly[(oos_monthly < train_mean - 2*train_std) | (oos_monthly > train_mean + 2*train_std)]
    print(f"  OOS months outside 2-sigma: {len(outliers)} out of {len(oos_monthly)}")
    if len(outliers) > 0:
        print(f"    Outlier months: {list(outliers.items())}")
