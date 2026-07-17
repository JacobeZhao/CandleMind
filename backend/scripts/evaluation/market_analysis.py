"""
市场结构分析脚本 — 合并 market_structure_analysis / market_structure_analysis2

用法:
  python -m backend.scripts.evaluation.market_analysis
  python -m backend.scripts.evaluation.market_analysis --symbol BNBUSDT
  python -m backend.scripts.evaluation.market_analysis --phase1
  python -m backend.scripts.evaluation.market_analysis --phase2

内容:
  Part 1. 特征分布: Hurst / ADX / Vol / Taker / Funding / EMA / Autocorr / TrendR2 / BB / 月度价格
  Part 2. 深度分析: EMA align flip / trend_confirm_4h / 标注不对称 / 自相关 regime / BNB 专项
"""
import sys, os, warnings, argparse
warnings.filterwarnings('ignore')
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import pandas as pd
import numpy as np
from app.datastore import MARKET_ROOT, FEATURES_ML_DIR, LABELS_DIR

TRAIN_START = '2023-01-01'
VAL_START   = '2025-07-01'
OOS_START   = '2026-01-01'

SEP = '=' * 70


def load_data(sym: str, feat_cols=None):
    feat_path  = FEATURES_ML_DIR / f'{sym}_features.parquet'
    label_path = LABELS_DIR / f'{sym}_5m_labels.parquet'

    lab_cols = ['open_time', 'close', 'atr', 'long_label', 'short_label']
    df_lab = pd.read_parquet(label_path, columns=lab_cols)

    if feat_cols:
        cols = list(dict.fromkeys(['open_time'] + feat_cols))
        df_feat = pd.read_parquet(feat_path, columns=cols)
    else:
        df_feat = pd.read_parquet(feat_path)

    for frame in [df_feat, df_lab]:
        if frame['open_time'].dtype.kind != 'M':
            frame['dt'] = pd.to_datetime(frame['open_time'], unit='ms')
        else:
            frame['dt'] = frame['open_time']

    df = df_feat.merge(df_lab[['open_time', 'close', 'atr', 'long_label', 'short_label']],
                       on='open_time', how='inner')
    print(f"Merged {len(df):,} rows: {df['dt'].min().date()} ~ {df['dt'].max().date()}")
    return df


def split(df):
    train = df[(df['dt'] >= TRAIN_START) & (df['dt'] < VAL_START)]
    val   = df[(df['dt'] >= VAL_START)   & (df['dt'] < OOS_START)]
    oos   = df[df['dt'] >= OOS_START]
    return train, val, oos


def stats(series, name):
    s = series.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) == 0:
        return f'  {name}: NO DATA'
    return (f'  {name}: mean={s.mean():.4f}  med={s.median():.4f}  std={s.std():.4f}  '
            f'p25={s.quantile(.25):.4f}  p75={s.quantile(.75):.4f}  N={len(s)}')


def part1(sym: str):
    print(f'\n{SEP}')
    print(f'  PART 1 — {sym} — 特征分布分析')
    print(f'{SEP}')

    feat_cols = [
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

    df = load_data(sym, feat_cols)
    train, val, oos = split(df)
    print(f'Train: {len(train)}, Val: {len(val)}, OOS: {len(oos)}')

    print(f'\n{SEP}\n=== 1. Hurst Exponent ===\n{SEP}')
    for col in ['5m_hurst', '1h_hurst', '4h_hurst', '1d_hurst']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'    {nm}: Hurst>0.5(trend)={(s > 0.5).mean():.1%}  Hurst<0.5(MR)={(s < 0.5).mean():.1%}')

    print(f'\n{SEP}\n=== 2. ADX ===\n{SEP}')
    for col in ['5m_adx', '1h_adx', '4h_adx']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'    {nm}: ADX>25={(s > 25).mean():.1%}  ADX>35={(s > 35).mean():.1%}  ADX<20={(s < 20).mean():.1%}')

    print(f'\n{SEP}\n=== 3. Volatility ===\n{SEP}')
    for col in ['5m_vol_regime', '1h_vol_regime', '4h_vol_regime',
                '5m_atr_pct20', '1h_atr_pct20', '4h_atr_pct20']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))

    print(f'\n{SEP}\n=== 4. Taker Buy Ratio ===\n{SEP}')
    for col in ['5m_taker_buy_ratio', '1h_taker_buy_ratio', '4h_taker_buy_ratio']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'    {nm}: >0.55={(s > 0.55).mean():.1%}  <0.45={(s < 0.45).mean():.1%}')

    print(f'\n{SEP}\n=== 5. Funding Rate ===\n{SEP}')
    for col in ['5m_funding_rate', '5m_funding_crowded_long', '5m_funding_crowded_short']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))

    print(f'\n{SEP}\n=== 6. EMA Alignment Score ===\n{SEP}')
    for col in ['5m_ema_align_score', '1h_ema_align_score', '4h_ema_align_score']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'    {nm}: bull_align(>=0.8)={(s >= 0.8).mean():.1%}'
                  f'  bear_align(<=-0.8)={(s <= -0.8).mean():.1%}'
                  f'  neutral={(s.abs() < 0.3).mean():.1%}')

    print(f'\n{SEP}\n=== 7. Autocorrelation ===\n{SEP}')
    for col in ['5m_autocorr_1', '1h_autocorr_1', '4h_autocorr_1']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'    {nm}: momentum(>0.05)={(s > 0.05).mean():.1%}  mean-revert(<-0.05)={(s < -0.05).mean():.1%}')

    print(f'\n{SEP}\n=== 8. Trend R2 ===\n{SEP}')
    for col in ['5m_trend_r2_20', '1h_trend_r2_20', '4h_trend_r2_20']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))

    print(f'\n{SEP}\n=== 9. Bollinger Band Width ===\n{SEP}')
    for col in ['5m_bb_width_20', '1h_bb_width_20', '4h_bb_width_20']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))

    print(f'\n{SEP}\n=== 10. Monthly Price Direction (OOS) ===\n{SEP}')
    df_oos = oos.copy()
    df_oos['month'] = df_oos['dt'].dt.to_period('M')

    def direction(x):
        if x > 0.02:
            return 'UP'
        elif x < -0.02:
            return 'DN'
        return 'FLAT'

    for period_name, period_df in [('OOS', df_oos), ('Train', train.copy())]:
        period_df['month'] = period_df['dt'].dt.to_period('M')
        monthly = period_df.groupby('month').agg(
            open_p=('close', 'first'), close_p=('close', 'last'),
            atr_mean=('atr', 'mean'),
            long_lbl_rate=('long_label', 'mean'),
            short_lbl_rate=('short_label', 'mean'),
        ).reset_index()
        monthly['ret'] = (monthly['close_p'] - monthly['open_p']) / monthly['open_p']
        monthly['dir'] = monthly['ret'].apply(direction)
        print(f'\n--- {period_name} monthly ---')
        print(monthly[['month', 'ret', 'dir', 'atr_mean', 'long_lbl_rate', 'short_lbl_rate']].to_string(index=False))
        up = (monthly['dir'] == 'UP').sum()
        dn = (monthly['dir'] == 'DN').sum()
        total = len(monthly)
        print(f'Summary: UP={up}/{total}({up/total:.0%})  DN={dn}/{total}({dn/total:.0%})')

    print(f'\n{SEP}\n=== 11. Interaction Features ===\n{SEP}')
    for col in ['ia_adx_mom_1h', 'ia_vol_atr_1h', 'ia_decouple_1h', 'ia_trend_confirm_4h']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        print(stats(train[col], 'Train'))
        print(stats(val[col],   'Val  '))
        print(stats(oos[col],   'OOS  '))

    print(f'\n{SEP}\n=== 12. Label Rates ===\n{SEP}')
    for nm, subset in [('Train', train), ('Val', val), ('OOS', oos)]:
        ll = subset['long_label'].mean()
        sl = subset['short_label'].mean()
        print(f'  {nm}: long_label={ll:.2%}  short_label={sl:.2%}')


def part2(sym: str):
    print(f'\n{SEP}')
    print(f'  PART 2 — {sym} — Regime Shift 深度分析')
    print(f'{SEP}')

    feat_cols = [
        '1h_ema_align_score', '4h_ema_align_score',
        '1h_adx', '4h_adx',
        '5m_autocorr_1', '1h_autocorr_1',
        '5m_atr_pct20', '4h_atr_pct20',
        'ia_trend_confirm_4h',
        '5m_trend_r2_20', '4h_trend_r2_20',
        '5m_rv24', '4h_rv24',
        '5m_taker_buy_ratio',
    ]

    df = load_data(sym, feat_cols)
    train, val, oos = split(df)

    print(f'\n{SEP}\nA. EMA Alignment Directional Bias\n{SEP}')
    for col in ['1h_ema_align_score', '4h_ema_align_score']:
        if col not in df.columns:
            continue
        print(f'\n[{col}]')
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'  {nm}: mean={s.mean():+.3f}  bull>0={(s > 0).mean():.1%}'
                  f'  bear<0={(s < 0).mean():.1%}  neutral=0={(s == 0).mean():.1%}')

    if 'ia_trend_confirm_4h' in df.columns:
        print(f'\n{SEP}\nB. ia_trend_confirm_4h Activation\n{SEP}')
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub['ia_trend_confirm_4h'].replace([np.inf, -np.inf], np.nan).dropna()
            tr_base = train['ia_trend_confirm_4h'].mean()
            print(f'  {nm}: {s.mean():.1%}  (Train baseline={tr_base:.1%})')

    print(f'\n{SEP}\nC. Label Asymmetry vs Price Direction (monthly OOS)\n{SEP}')
    df_oos = oos.copy()
    df_oos['month'] = df_oos['dt'].dt.to_period('M')
    agg = {'open_p': ('close', 'first'), 'close_p': ('close', 'last'),
           'long_r': ('long_label', 'mean'), 'short_r': ('short_label', 'mean')}
    if '4h_ema_align_score' in df.columns:
        agg['ema4h'] = ('4h_ema_align_score', 'mean')
    if '4h_adx' in df.columns:
        agg['adx4h'] = ('4h_adx', 'mean')
    monthly = df_oos.groupby('month').agg(**agg).reset_index()
    monthly['price_ret'] = (monthly['close_p'] - monthly['open_p']) / monthly['open_p']
    monthly['lbl_bias']  = monthly['long_r'] - monthly['short_r']
    monthly['price_dir'] = monthly['price_ret'].apply(
        lambda x: 'UP' if x > 0.02 else ('DN' if x < -0.02 else 'FLAT'))
    monthly['aligned'] = monthly.apply(
        lambda r: 'OK' if (r['price_dir'] == 'UP' and r['lbl_bias'] > 0)
                       or (r['price_dir'] == 'DN' and r['lbl_bias'] < 0)
                  else 'MISMATCH', axis=1)
    print(monthly.to_string(index=False))
    mismatches = (monthly['aligned'] == 'MISMATCH').sum()
    print(f'\n  OOS signal mismatches = {mismatches}/{len(monthly)} ({mismatches/max(len(monthly),1):.0%})')

    print(f'\n{SEP}\nD. Autocorrelation Regime Shift\n{SEP}')
    for col in ['5m_autocorr_1', '1h_autocorr_1']:
        if col not in df.columns:
            continue
        for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
            s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) == 0:
                continue
            print(f'  {nm} [{col}]: mean={s.mean():.4f}  MR(<-0.05)={(s < -0.05).mean():.1%}'
                  f'  MOM(>0.05)={(s > 0.05).mean():.1%}')

    print(f'\n{SEP}\nE. Volatility Contraction\n{SEP}')
    for nm, sub in [('Train', train), ('Val', val), ('OOS', oos)]:
        a5 = sub['5m_atr_pct20'].replace([np.inf, -np.inf], np.nan).dropna() if '5m_atr_pct20' in sub else None
        a4 = sub['4h_atr_pct20'].replace([np.inf, -np.inf], np.nan).dropna() if '4h_atr_pct20' in sub else None
        rv = sub['5m_rv24'].replace([np.inf, -np.inf], np.nan).dropna() if '5m_rv24' in sub else None
        print(f'  {nm}: 5m_ATR%={"N/A" if a5 is None else f"{a5.mean():.4f}"}'
              f'  4h_ATR%={"N/A" if a4 is None else f"{a4.mean():.4f}"}'
              f'  5m_rv24={"N/A" if rv is None else f"{rv.mean():.4f}"}')

    # BNB specific (only when not already analyzing BNB)
    if sym != 'BNBUSDT':
        print(f'\n{SEP}\nF. BNB-specific Analysis\n{SEP}')
        try:
            bnb_feat_cols = ['open_time', '1h_ema_align_score', '4h_ema_align_score',
                             '1h_adx', '4h_adx', '5m_taker_buy_ratio',
                             '5m_funding_rate', 'ia_trend_confirm_4h',
                             '5m_autocorr_1', '4h_trend_r2_20']
            bnb_lab_cols  = ['open_time', 'close', 'atr', 'long_label', 'short_label']
            bnb_feat_path  = FEATURES_ML_DIR / 'BNBUSDT_features.parquet'
            bnb_label_path = LABELS_DIR / 'BNBUSDT_5m_labels.parquet'
            bnb_f = pd.read_parquet(bnb_feat_path, columns=bnb_feat_cols)
            bnb_l = pd.read_parquet(bnb_label_path, columns=bnb_lab_cols)
            for fr in [bnb_f, bnb_l]:
                fr['dt'] = pd.to_datetime(fr['open_time'], unit='ms') if fr['open_time'].dtype.kind != 'M' else fr['open_time']
            bnb = bnb_f.merge(bnb_l[['open_time', 'close', 'long_label', 'short_label']], on='open_time', how='inner')
            bnb_tr  = bnb[(bnb['dt'] >= TRAIN_START) & (bnb['dt'] < VAL_START)]
            bnb_oos = bnb[bnb['dt'] >= OOS_START]
            print(f'  BNB rows: train={len(bnb_tr)}, OOS={len(bnb_oos)}')
            for col in ['1h_ema_align_score', '4h_ema_align_score', 'ia_trend_confirm_4h']:
                if col not in bnb.columns:
                    continue
                for nm, sub in [('Train', bnb_tr), ('OOS', bnb_oos)]:
                    s = sub[col].replace([np.inf, -np.inf], np.nan).dropna()
                    if len(s) == 0:
                        continue
                    print(f'  BNB [{col}] {nm}: mean={s.mean():+.3f}'
                          f'  bull={(s > 0).mean():.1%}  bear={(s < 0).mean():.1%}')
        except Exception as e:
            print(f'  BNB load error: {e}')


def main():
    parser = argparse.ArgumentParser(description='Market structure analysis')
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--phase1', action='store_true')
    parser.add_argument('--phase2', action='store_true')
    args = parser.parse_args()

    run_both = not args.phase1 and not args.phase2

    if args.phase1 or run_both:
        part1(args.symbol)
    if args.phase2 or run_both:
        part2(args.symbol)


if __name__ == '__main__':
    main()
