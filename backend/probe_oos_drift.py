"""
OOS概率漂移诊断脚本 — 内存优化版
分析BTC/BNB模型在训练期 vs OOS期的概率分布、校准质量和信号漂移
"""
import sys, os, warnings, gc
warnings.filterwarnings('ignore')
sys.path.insert(0, r'E:\File\Projects\CandleMind\backend')
os.chdir(r'E:\File\Projects\CandleMind\backend')

import pandas as pd
import numpy as np
from sklearn.metrics import roc_auc_score
import pickle

def load_bundle(symbol, target):
    model_path = rf'G:\5、金融交易\models\{symbol}_{target}.pkl'
    with open(model_path, 'rb') as f:
        return pickle.load(f)

def calibration_buckets(probs, labels, n_buckets=5):
    df = pd.DataFrame({'prob': probs, 'label': labels})
    try:
        df['bucket'] = pd.qcut(df['prob'], q=n_buckets, duplicates='drop', labels=False)
    except Exception:
        df['bucket'] = pd.cut(df['prob'], bins=n_buckets, labels=False)
    rows = []
    for b, g in df.groupby('bucket'):
        rows.append({
            'bucket': int(b),
            'mean_pred': round(g['prob'].mean(), 4),
            'mean_actual': round(g['label'].mean(), 4),
            'n': len(g),
        })
    return rows

def to_dt(col):
    if col.dtype.kind == 'M':
        return col
    return pd.to_datetime(col, unit='ms')

# ══════════════════════════════════════════════════════════════
# 主分析
# ══════════════════════════════════════════════════════════════
for sym, entry_thr in [('BTCUSDT', 0.62), ('BNBUSDT', 0.58)]:
    print(f"\n{'='*65}")
    print(f"  [{sym}]  entry_threshold={entry_thr}")
    print(f"{'='*65}")

    feat_path  = rf'G:\5、金融交易\features_ml\{sym}_features.parquet'
    label_path = rf'G:\5、金融交易\labels\{sym}_5m_labels.parquet'

    # 先读时间列，确认数据范围
    feat_meta = pd.read_parquet(feat_path, columns=['open_time'])
    feat_meta['dt'] = to_dt(feat_meta['open_time'])
    print(f"  特征文件时间范围: {feat_meta['dt'].min().date()} ~ {feat_meta['dt'].max().date()}")
    print(f"  总bar数: {len(feat_meta):,}")

    labels = pd.read_parquet(label_path, columns=['open_time','long_label','short_label'])
    labels['dt'] = to_dt(labels['open_time'])
    del feat_meta
    gc.collect()

    # ── 按 target 分别处理（各自只加载需要的特征列）──
    for target, col, thr in [('long_label','long_prob', entry_thr),
                              ('short_label','short_prob', entry_thr)]:
        print(f"\n  ── {target} ──")
        try:
            bundle = load_bundle(sym, target)
        except FileNotFoundError as e:
            print(f"    模型未找到: {e}")
            continue
        except Exception as e:
            print(f"    加载失败: {e}")
            continue

        print(f"    feature_cols数量: {len(bundle.feature_cols)}")

        # 只读取模型需要的列 + open_time
        read_cols = list(dict.fromkeys(['open_time'] + bundle.feature_cols))
        # 检查特征文件实际有哪些列
        import pyarrow.parquet as pq
        all_feat_cols = pq.read_schema(feat_path).names
        avail = [c for c in read_cols if c in all_feat_cols]
        missing_feats = [c for c in bundle.feature_cols if c not in all_feat_cols]
        if missing_feats:
            print(f"    WARN: 特征文件中缺失 {len(missing_feats)} 列: {missing_feats[:5]}...")

        # 读取特征（只需要的列）— 确保open_time在内
        if 'open_time' not in avail:
            avail = ['open_time'] + avail
        feats_slim = pd.read_parquet(feat_path, columns=avail)
        feats_slim['dt'] = to_dt(feats_slim['open_time'])

        # 合并标签
        merged = feats_slim.merge(
            labels[['open_time', target]],
            on='open_time', how='inner'
        )
        merged['dt'] = to_dt(merged['open_time'])

        # 补全缺失特征列（用0填充）
        for mc in bundle.feature_cols:
            if mc not in merged.columns:
                merged[mc] = 0.0

        del feats_slim
        gc.collect()

        results = {}
        for name, mask in [
            ('训练期(2023-01~2024-06)', (merged['dt'] >= '2023-01-01') & (merged['dt'] < '2024-07-01')),
            ('验证期(2024-07~2024-12)', (merged['dt'] >= '2024-07-01') & (merged['dt'] < '2025-01-01')),
            ('OOS(2025-01~2026-06)',   merged['dt'] >= '2025-01-01'),
        ]:
            subset = merged[mask]
            if len(subset) < 100:
                print(f"    {name}: 数据量不足({len(subset)})，跳过")
                continue

            X = subset[bundle.feature_cols].fillna(0).astype(np.float32)
            probs = bundle.predict_proba(X)
            y = subset[target].values

            del X
            gc.collect()

            auc = roc_auc_score(y, probs) if len(np.unique(y)) > 1 else float('nan')
            above_thr = (probs >= thr).mean()
            pos_rate = y.mean()
            pct = np.percentile(probs, [10, 25, 50, 75, 90])

            results[name] = {
                'auc': auc, 'mean': probs.mean(), 'std': probs.std(),
                'p10': pct[0], 'p25': pct[1], 'p50': pct[2],
                'p75': pct[3], 'p90': pct[4],
                'above_thr': above_thr, 'pos_rate': pos_rate,
                'n': len(subset),
            }

            print(f"    {name}  n={len(subset):,}:")
            print(f"      AUC={auc:.4f}  mean={probs.mean():.4f}  std={probs.std():.4f}")
            print(f"      p10={pct[0]:.4f}  p25={pct[1]:.4f}  p50={pct[2]:.4f}  p75={pct[3]:.4f}  p90={pct[4]:.4f}")
            print(f"      prob>={thr:.2f}: {above_thr:.3f} ({int(above_thr*len(subset))}次)  label_pos_rate={pos_rate:.4f}")

            # 校准桶
            cal = calibration_buckets(probs, y, n_buckets=5)
            print(f"      校准(5桶): ", end='')
            for b in cal:
                diff = b['mean_actual'] - b['mean_pred']
                print(f"[pred={b['mean_pred']:.3f}→act={b['mean_actual']:.3f},Δ={diff:+.3f},n={b['n']}]", end=' ')
            print()

            del probs, y
            gc.collect()

        # AUC 漂移汇总
        k_train = '训练期(2023-01~2024-06)'
        k_oos   = 'OOS(2025-01~2026-06)'
        k_val   = '验证期(2024-07~2024-12)'
        if k_train in results and k_oos in results:
            print(f"\n    >>> AUC漂移:      {results[k_train]['auc']:.4f}(训练) → {results.get(k_val,{}).get('auc','N/A')} (验证) → {results[k_oos]['auc']:.4f}(OOS)")
            print(f"    >>> mean_prob漂移: {results[k_train]['mean']:.4f}(训练) → {results[k_oos]['mean']:.4f}(OOS)  Δ={results[k_oos]['mean']-results[k_train]['mean']:+.4f}")
            print(f"    >>> 超阈值频率:    {results[k_train]['above_thr']:.3f}(训练) → {results[k_oos]['above_thr']:.3f}(OOS)  Δ={results[k_oos]['above_thr']-results[k_train]['above_thr']:+.3f}")

        del merged
        gc.collect()

    # ══════════════════════════════════════════════════════════════
    # BNB 专项：空头月度信号 vs 价格走势
    # ══════════════════════════════════════════════════════════════
    if sym == 'BNBUSDT':
        print(f"\n  {'─'*55}")
        print(f"  [BNB 空头信号月度分布 — 核心诊断]")
        print(f"  {'─'*55}")
        try:
            bundle_s = load_bundle(sym, 'short_label')
            bundle_l = load_bundle(sym, 'long_label')

            # 读取需要的列：取两个模型特征的并集 + 必要列
            need_cols = list(dict.fromkeys(
                ['open_time'] +
                bundle_s.feature_cols +
                bundle_l.feature_cols
            ))
            import pyarrow.parquet as pq
            all_feat_cols_bnb = pq.read_schema(feat_path).names
            read_cols = [c for c in need_cols if c in all_feat_cols_bnb]
            if 'open_time' not in read_cols:
                read_cols = ['open_time'] + read_cols
            # 如果特征文件有close列，也读入
            if 'close' in all_feat_cols_bnb and 'close' not in read_cols:
                read_cols.append('close')

            feats_bnb = pd.read_parquet(feat_path, columns=read_cols)
            feats_bnb['dt'] = to_dt(feats_bnb['open_time'])

            lbl_bnb = pd.read_parquet(label_path, columns=['open_time','long_label','short_label'])
            merged_bnb = feats_bnb.merge(lbl_bnb, on='open_time', how='inner')
            merged_bnb['dt'] = to_dt(merged_bnb['open_time'])

            del feats_bnb, lbl_bnb
            gc.collect()

            # 补全缺失列
            for mc in bundle_s.feature_cols + bundle_l.feature_cols:
                if mc not in merged_bnb.columns:
                    merged_bnb[mc] = 0.0

            # 计算概率
            X_s = merged_bnb[bundle_s.feature_cols].fillna(0).astype(np.float32)
            merged_bnb['short_prob'] = bundle_s.predict_proba(X_s)
            del X_s; gc.collect()

            X_l = merged_bnb[bundle_l.feature_cols].fillna(0).astype(np.float32)
            merged_bnb['long_prob'] = bundle_l.predict_proba(X_l)
            del X_l; gc.collect()

            merged_bnb['month'] = merged_bnb['dt'].dt.to_period('M')

            # OOS月度汇总
            oos_bnb = merged_bnb[merged_bnb['dt'] >= '2025-01-01'].copy()
            print(f"\n  月度统计 (BNB OOS):")
            hdr = f"  {'月份':<10} {'close均价':>10} {'short_mean':>10} {'short>0.58':>10} {'long_mean':>10} {'long>0.58':>10} {'sl_rate':>8} {'ll_rate':>8}"
            print(hdr)
            print(f"  {'-'*85}")

            for month, g in oos_bnb.groupby('month'):
                close_info = f"{g['close'].mean():.1f}" if 'close' in g.columns else 'N/A'
                s_mean  = g['short_prob'].mean()
                s_above = (g['short_prob'] >= 0.58).mean()
                l_mean  = g['long_prob'].mean()
                l_above = (g['long_prob'] >= 0.58).mean()
                sl_rate = g['short_label'].mean()
                ll_rate = g['long_label'].mean()
                print(f"  {str(month):<10} {close_info:>10} {s_mean:>10.4f} {s_above:>10.3f} {l_mean:>10.4f} {l_above:>10.3f} {sl_rate:>8.4f} {ll_rate:>8.4f}  n={len(g):,}")

            # 训练期对比基线
            train_bnb = merged_bnb[(merged_bnb['dt'] >= '2023-01-01') & (merged_bnb['dt'] < '2024-07-01')]
            print(f"\n  训练期基线 (2023-01~2024-06):")
            print(f"    short_prob mean={train_bnb['short_prob'].mean():.4f}  >0.58: {(train_bnb['short_prob']>=0.58).mean():.3f}")
            print(f"    long_prob  mean={train_bnb['long_prob'].mean():.4f}  >0.58: {(train_bnb['long_prob']>=0.58).mean():.3f}")
            print(f"    short_label_rate={train_bnb['short_label'].mean():.4f}  long_label_rate={train_bnb['long_label'].mean():.4f}")

            # 校准精度：high_short_prob时实际命中率
            print(f"\n  [校准诊断] OOS期 short_prob>0.58 时的实际short_label命中率:")
            high_short = oos_bnb[oos_bnb['short_prob'] >= 0.58]
            if len(high_short) > 0:
                actual_prec = high_short['short_label'].mean()
                mean_prob_hs = high_short['short_prob'].mean()
                print(f"    n={len(high_short):,}  mean_prob={mean_prob_hs:.4f}  实际命中率={actual_prec:.4f}  过度自信={mean_prob_hs-actual_prec:+.4f}")

            print(f"\n  [校准诊断] OOS期 long_prob>0.58 时的实际long_label命中率:")
            high_long = oos_bnb[oos_bnb['long_prob'] >= 0.58]
            if len(high_long) > 0:
                actual_prec_l = high_long['long_label'].mean()
                mean_prob_hl = high_long['long_prob'].mean()
                print(f"    n={len(high_long):,}  mean_prob={mean_prob_hl:.4f}  实际命中率={actual_prec_l:.4f}  过度自信={mean_prob_hl-actual_prec_l:+.4f}")

            # 价格单调上涨但空头信号依然强 — 检查特征变化
            if 'close' in oos_bnb.columns:
                print(f"\n  [价格 vs 信号] 2025年BNB价格区间:")
                for period, g in oos_bnb.groupby('month'):
                    if len(g) > 100:
                        first_close = g['close'].iloc[0]
                        last_close  = g['close'].iloc[-1]
                        ret = (last_close - first_close) / first_close
                        print(f"    {period}: {first_close:.1f} -> {last_close:.1f}  月收益={ret:+.2%}  "
                              f"short_prob_mean={g['short_prob'].mean():.4f}  long_prob_mean={g['long_prob'].mean():.4f}")

            del merged_bnb, oos_bnb, train_bnb
            gc.collect()

        except Exception as e:
            import traceback
            print(f"  BNB专项分析失败: {e}")
            traceback.print_exc()

print(f"\n{'='*65}")
print("  分析完成")
print(f"{'='*65}")
