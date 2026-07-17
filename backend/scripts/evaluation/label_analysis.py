"""
标注质量分析脚本 — 合并 analyze_labels / analyze_labels2 / analyze_labels3

用法:
  python -m backend.scripts.evaluation.label_analysis
  python -m backend.scripts.evaluation.label_analysis --symbol BNBUSDT
  python -m backend.scripts.evaluation.label_analysis --symbol all

分析内容:
  Part 1. 基础统计: 正样本率、连续段长度、多空相关性、regime 分布
  Part 2. 细节统计: barrier_hit、profit_r、duration、trend quality、MAE、meta label
  Part 3. 标注稳健性: base-rate edge、月度不对称、BNB 分析、平稳性检验
"""
import sys, os, warnings, argparse
warnings.filterwarnings('ignore')
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import pandas as pd
import numpy as np
from app.datastore import MARKET_ROOT, LABELS_DIR

DEFAULT_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT']

TRAIN_START = '2023-01-01'
TRAIN_END   = '2025-06-30'
VAL_START   = '2025-07-01'
VAL_END     = '2025-12-31'
OOS_START   = '2026-01-01'


def load_labels(sym: str) -> pd.DataFrame:
    path = LABELS_DIR / f'{sym}_5m_labels.parquet'
    df = pd.read_parquet(path)
    if 'open_time' in df.columns:
        if df['open_time'].dtype.kind != 'M':
            df['dt'] = pd.to_datetime(df['open_time'], unit='ms')
        else:
            df['dt'] = df['open_time']
    else:
        df['dt'] = df.index
    df['month'] = df['dt'].dt.to_period('M')
    return df


def split(df):
    train = df[(df['dt'] >= TRAIN_START) & (df['dt'] < VAL_START)]
    val   = df[(df['dt'] >= VAL_START)   & (df['dt'] <= VAL_END)]
    oos   = df[df['dt'] >= OOS_START]
    return train, val, oos


def analyze(sym: str):
    print(f'\n{"=" * 70}')
    print(f'  {sym}')
    print(f'{"=" * 70}')

    df = load_labels(sym)
    train, val, oos = split(df)
    print(f"total: {len(df):,}  range: {df['dt'].min().date()} ~ {df['dt'].max().date()}")
    print(f"train: {len(train):,}  val: {len(val):,}  OOS: {len(oos):,}")

    # ── Part 1: 基础统计 ────────────────────────────────────────────────
    print(f'\n--- Part 1: 正样本率 ---')
    for col in ['long_label', 'short_label']:
        if col not in df.columns:
            continue
        print(f'\n  {col}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            rate = subset[col].mean()
            print(f'    {name}: positive_rate={rate:.3f}  n={len(subset):,}')

        monthly = df.groupby('month')[col].mean()
        print(f'  monthly positive rates:')
        for m, r in monthly.items():
            print(f'    {m}: {r:.3f}')

    print(f'\n--- Part 1: 连续段长度 ---')
    for col in ['long_label', 'short_label']:
        if col not in df.columns:
            continue
        for name, subset in [('train', train), ('OOS', oos)]:
            if len(subset) == 0:
                continue
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
                print(f'  {col} {name}: mean={runs.mean():.2f} median={np.median(runs):.1f}'
                      f' max={runs.max()} p75={np.percentile(runs, 75):.1f}'
                      f' p90={np.percentile(runs, 90):.1f}')

    if 'long_label' in df.columns and 'short_label' in df.columns:
        print(f'\n--- Part 1: 多空相关性 ---')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            corr = subset['long_label'].corr(subset['short_label'])
            both = subset[(subset['long_label'] == 1) & (subset['short_label'] == 1)]
            none = subset[(subset['long_label'] == 0) & (subset['short_label'] == 0)]
            print(f'  {name}: corr={corr:.4f}  both=1:{len(both)/len(subset):.4f}'
                  f'  both=0:{len(none)/len(subset):.4f}')

    if 'regime' in df.columns:
        print(f'\n--- Part 1: regime 分布 ---')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            print(f'  {name}:')
            print(subset['regime'].value_counts(normalize=True).to_string())

    # ── Part 2: 细节统计 ────────────────────────────────────────────────
    print(f'\n--- Part 2: Barrier Hit ---')
    for col in ['long_barrier_hit', 'short_barrier_hit']:
        if col not in df.columns:
            continue
        print(f'\n  {col}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            vc = subset[col].value_counts(normalize=True)
            print(f'    {name}: {vc.to_dict()}')

    print(f'\n--- Part 2: Profit R stats ---')
    for col, lbl in [('long_profit_r', 'long_label'), ('short_profit_r', 'short_label')]:
        if col not in df.columns or lbl not in df.columns:
            continue
        print(f'\n  {col}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            wins   = subset[subset[lbl] == 1][col]
            losses = subset[subset[lbl] == 0][col]
            print(f'    {name} wins : mean={wins.mean():.3f} std={wins.std():.3f} n={len(wins):,}')
            print(f'    {name} losses: mean={losses.mean():.3f} std={losses.std():.3f} n={len(losses):,}')

    print(f'\n--- Part 2: Duration Bars ---')
    for col, lbl in [('long_duration_bars', 'long_label'), ('short_duration_bars', 'short_label')]:
        if col not in df.columns or lbl not in df.columns:
            continue
        print(f'\n  {col}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            pos = subset[subset[lbl] == 1][col]
            print(f'    {name}: mean={pos.mean():.1f} median={pos.median():.1f}'
                  f' p75={pos.quantile(0.75):.1f} p90={pos.quantile(0.90):.1f}')

    print(f'\n--- Part 2: Trend Quality ---')
    for direction in ['long', 'short']:
        lbl         = f'{direction}_label'
        sharpe_col  = f'{direction}_trend_sharpe'
        consist_col = f'{direction}_trend_consistency'
        if lbl not in df.columns or sharpe_col not in df.columns:
            continue
        print(f'\n  {direction}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            pos = subset[subset[lbl] == 1]
            print(f'    {name}: sharpe={pos[sharpe_col].mean():.3f}'
                  f'  consistency={pos[consist_col].mean():.3f}')

    print(f'\n--- Part 2: Max Adverse R (正样本 TP 前最大逆境) ---')
    for direction in ['long', 'short']:
        lbl     = f'{direction}_label'
        mae_col = f'{direction}_max_adverse_r'
        if lbl not in df.columns or mae_col not in df.columns:
            continue
        print(f'\n  {direction}:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            pos = subset[subset[lbl] == 1][mae_col]
            print(f'    {name}: mean={pos.mean():.3f} p75={pos.quantile(0.75):.3f}'
                  f' p90={pos.quantile(0.90):.3f}')

    if 'regime_profit_mult' in df.columns:
        print(f'\n--- Part 2: regime_profit_mult ---')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            rpm = subset['regime_profit_mult']
            print(f'  {name}: mean={rpm.mean():.3f} median={rpm.median():.3f} std={rpm.std():.3f}')

    if 'best_direction' in df.columns:
        print(f'\n--- Part 2: best_direction ---')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            print(f'  {name}: {subset["best_direction"].value_counts(normalize=True).to_dict()}')

    meta_cols = ['long_meta_label', 'short_meta_label']
    if any(c in df.columns for c in meta_cols):
        print(f'\n--- Part 2: Meta Label Rates ---')
        for col in meta_cols:
            if col not in df.columns:
                continue
            print(f'\n  {col}:')
            for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
                if len(subset) == 0:
                    continue
                print(f'    {name}: {subset[col].mean():.3f}  n={len(subset):,}')

    # ── Part 3: 稳健性分析 ────────────────────────────────────────────
    print(f'\n--- Part 3: Base-rate edge ---')
    for direction in ['long', 'short']:
        lbl        = f'{direction}_label'
        profit_col = f'{direction}_profit_r'
        if lbl not in df.columns or profit_col not in df.columns:
            continue
        print(f'\n  {direction} oracle strategy:')
        for name, subset in [('train', train), ('val', val), ('OOS', oos)]:
            if len(subset) == 0:
                continue
            pos = subset[subset[lbl] == 1]
            neg = subset[subset[lbl] == 0]
            avg_r_pos = pos[profit_col].mean()
            avg_r_neg = neg[profit_col].mean()
            print(f'    {name}: E[R|label=1]={avg_r_pos:.3f}  E[R|label=0]={avg_r_neg:.3f}'
                  f'  pos_rate={len(pos)/(len(pos)+len(neg)):.3f}')

    print(f'\n--- Part 3: 月度不对称 (OOS, short_rate - long_rate) ---')
    if 'long_label' in df.columns and 'short_label' in df.columns and len(oos) > 0:
        agg = {'long_rate': ('long_label', 'mean'), 'short_rate': ('short_label', 'mean'),
               'n': ('long_label', 'count')}
        for col in ['long_meta_label', 'short_meta_label']:
            if col in df.columns:
                agg[col.replace('_label', '')] = (col, 'mean')
        oos_m = oos.groupby('month').agg(**agg).reset_index()
        oos_m['short_bias'] = oos_m['short_rate'] - oos_m['long_rate']
        print(oos_m.to_string(index=False))

    print(f'\n--- Part 3: 平稳性 ---')
    for col in ['long_label', 'short_label']:
        if col not in df.columns:
            continue
        monthly = df.groupby('month')[col].mean()
        train_m = monthly[monthly.index < pd.Period(VAL_START[:7], 'M')]
        oos_m   = monthly[monthly.index >= pd.Period(OOS_START[:7], 'M')]
        if len(oos_m) == 0:
            continue
        tm, ts = train_m.mean(), train_m.std()
        outliers = oos_m[(oos_m < tm - 2 * ts) | (oos_m > tm + 2 * ts)]
        print(f'  {col}: train_mean={tm:.4f} train_std={ts:.4f}'
              f'  OOS_mean={oos_m.mean():.4f}  OOS 2-sigma outliers:{len(outliers)}/{len(oos_m)}')
        if len(outliers) > 0:
            print(f'    Outlier months: {list(outliers.items()[:5])}')

    print(f'\nAll columns: {list(df.columns)}')


def main():
    parser = argparse.ArgumentParser(description='Label quality analysis')
    parser.add_argument('--symbol', default='BTCUSDT',
                        help='Symbol or "all" for all default symbols')
    args = parser.parse_args()

    if args.symbol == 'all':
        for s in DEFAULT_SYMBOLS:
            analyze(s)
    else:
        analyze(args.symbol)


if __name__ == '__main__':
    main()
