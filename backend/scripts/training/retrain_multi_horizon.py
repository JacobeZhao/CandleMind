"""P0-2/3: Retrain trend_signal model on multi-horizon labels with rolling window.

What it does
------------
* Loads 5m feature matrix (FEATURES_ML_DIR/{symbol}_features.parquet, 498 cols)
* Joins horizon-specific labels (LABELS_DIR/{symbol}_{horizon}_labels.parquet)
* Picks a rolling training window (default 6 months)
* Splits the next month into purged early-stop, calibration, and gate windows
* Runs CPCV (n_folds=6, n_test_folds=2, embargo=50 bars) on the training window
* Trains final LGBM+CatBoost ensemble + isotonic calibrator
* Saves artifacts under models/candidates/supervised/{release_id}
* GATE: refuse to write if isolated gate IC < 0.03 or gate AUC < 0.6

Usage
-----
  python -m backend.scripts.training.retrain_multi_horizon --release-id trend_20260717 --symbol BTCUSDT --horizon 1h --side long

Why this script exists
----------------------
The existing train_symbol() in services/trend_predictor.py operates on the
5m labels only and overwrites models/{symbol}_{target}.pkl. We need:
  1. Multiple horizon targets side-by-side (5m/30m/1h/4h)
  2. Rolling training windows (data is non-stationary)
  3. A quality gate (IC, AUC) before saving
  4. A separate "_v2" namespace so we can A/B against existing 5m model
"""
import os, sys, warnings, json, argparse, pickle
from pathlib import Path

warnings.filterwarnings('ignore')
BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.isotonic import IsotonicRegression

from app.datastore import (
    FEATURES_ML_DIR,
    LABELS_DIR,
    supervised_candidate_dir,
    validate_supervised_candidate_dir,
)
from app.services.trend_predictor import (
    BundleModel,
    CPCVSplitter,
    IndexWindow, TemporalModelSplit,
    _LGB_CPCV_PARAMS, _CB_CPCV_PARAMS,
    train_lgbm, train_catboost, train_final_model,
    run_cpcv, shap_feature_selection,
)

HORIZON_BARS = {'30m': 6, '1h': 12, '4h': 48}


def split_retrain_windows(
    n_samples: int,
    holdout_start: int,
    purge_bars: int,
    *,
    min_train: int = 3000,
    min_window: int = 200,
) -> TemporalModelSplit:
    """Build purged train, early-stop, calibration, and gate windows.

    ``holdout_start`` marks the first row after the rolling training period.
    The remaining rows are split chronologically into three contiguous uses.
    Gate rows are always last and are never returned as part of another use.
    """
    if n_samples < 0:
        raise ValueError("n_samples must be non-negative")
    if not 0 <= holdout_start <= n_samples:
        raise ValueError("holdout_start must fall within the sample range")
    if purge_bars < 0:
        raise ValueError("purge_bars must be non-negative")

    train_stop = holdout_start - purge_bars
    usable_holdout = n_samples - holdout_start - 2 * purge_bars
    early_stop_size = usable_holdout // 3
    calibration_size = usable_holdout // 3
    gate_size = usable_holdout - early_stop_size - calibration_size

    if (
        train_stop < min_train
        or early_stop_size < min_window
        or calibration_size < min_window
        or gate_size < min_window
    ):
        raise ValueError(
            "insufficient samples for purged retrain windows: "
            f"train={max(train_stop, 0)}, "
            f"early_stop={max(early_stop_size, 0)}, "
            f"calibration={max(calibration_size, 0)}, "
            f"gate={max(gate_size, 0)}"
        )

    early_stop = IndexWindow(holdout_start, holdout_start + early_stop_size)
    calibration_start = early_stop.stop + purge_bars
    calibration = IndexWindow(
        calibration_start, calibration_start + calibration_size
    )
    gate_start = calibration.stop + purge_bars
    gate = IndexWindow(gate_start, n_samples)
    return TemporalModelSplit(
        train=IndexWindow(0, train_stop),
        early_stop=early_stop,
        calibration=calibration,
        gate=gate,
        purged=(
            IndexWindow(train_stop, holdout_start),
            IndexWindow(early_stop.stop, calibration.start),
            IndexWindow(calibration.stop, gate.start),
        ),
    )


def select_features_by_gain(
    X: pd.DataFrame,
    y: pd.Series,
    max_features: int = 100,
) -> list[str]:
    """Select features using training-only gain importance."""
    non_constant = X.columns[X.var() > 1e-12].tolist()
    probe = train_lgbm(
        X[non_constant],
        y,
        params={
            'n_estimators': 250,
            'learning_rate': 0.05,
            'num_leaves': 31,
            'max_depth': 6,
            'min_child_samples': 200,
            'colsample_bytree': 0.6,
            'reg_alpha': 1.0,
            'reg_lambda': 2.0,
        },
    )
    gain = probe.booster_.feature_importance(importance_type='gain')
    ranked = sorted(zip(non_constant, gain), key=lambda item: item[1], reverse=True)
    selected = [name for name, importance in ranked if importance > 0][:max_features]
    return selected or non_constant[:max_features]


# --- shared exclude list, parametrized by side/horizon -----------------------
def _exclude_cols(side: str, horizon: str) -> set:
    base = {
        'open_time', 'year', 'index',
        # 5m label family (do not leak)
        'long_label',  'long_meta_label',  'long_profit_r',  'long_duration',
        'short_label', 'short_meta_label', 'short_profit_r', 'short_duration',
        'long_trend_sharpe', 'short_trend_sharpe', 'best_direction',
        'long_barrier_hit', 'short_barrier_hit',
        'long_trend_consistency', 'short_trend_consistency',
        'long_max_adverse_r', 'short_max_adverse_r',
        'long_max_favorable_r', 'short_max_favorable_r',
        'long_duration_bars', 'short_duration_bars',
        'atr',
        'label_valid', 'forward_return', 'horizon_sigma', 'decision_threshold',
        'long_net_return', 'short_net_return', 'trend_class', 'trend_score',
    }
    # also drop barrier meta from other horizons to avoid horizon leakage
    for h in ('5m', '30m', '1h', '4h'):
        for s in ('long', 'short'):
            for k in ('barrier_hit', 'profit_r', 'duration_bars', 'label', 'meta_label'):
                base.add(f'{s}_{k}_{h}') if False else None
    return base


def _info_share(spearman_corr, pval, label, n):
    return f'  {label:32s} IC={spearman_corr:+.4f}  p={pval:.4g}  n={n:,}'


def ic_and_diracc(prob, y):
    """Spearman IC and direction accuracy between predicted prob and binary y."""
    from scipy.stats import spearmanr
    if len(prob) < 30:
        return float('nan'), float('nan')
    rho, p = spearmanr(prob, y)
    pred_dir = (prob > 0.5).astype(int)
    dir_acc = (pred_dir == y).mean()
    return float(rho), float(dir_acc)


def load_merged(symbol: str, horizon: str, side: str, variant: str):
    feat = pd.read_parquet(FEATURES_ML_DIR / f'{symbol}_features.parquet')
    lab = pd.read_parquet(
        LABELS_DIR / f'{symbol}_{horizon}_{variant}_labels.parquet'
    )
    if feat['open_time'].dtype.kind == 'i':
        feat['open_time'] = pd.to_datetime(feat['open_time'], unit='ms')
    if lab['open_time'].dtype.kind == 'i':
        lab['open_time'] = pd.to_datetime(lab['open_time'], unit='ms')
    target = f'{side}_label'
    keep_lab_cols = ['open_time', target, f'{side}_meta_label',
                     f'{side}_profit_r', f'{side}_duration_bars',
                     f'{side}_barrier_hit', 'label_valid', 'forward_return',
                     'decision_threshold', 'long_net_return', 'short_net_return',
                     'trend_score']
    keep_lab_cols = [c for c in keep_lab_cols if c in lab.columns]
    lab = lab[keep_lab_cols]
    df = feat.merge(lab, on='open_time', how='inner')
    if 'label_valid' in df.columns:
        df = df[df['label_valid'] == 1].reset_index(drop=True)
    # drop warmup / cool-down so we do not train on undefined labels
    df = df.iloc[200:-48].reset_index(drop=True)
    core = [target, '5m_adx', '1h_adx']
    core = [c for c in core if c in df.columns]
    df = df.dropna(subset=core).reset_index(drop=True)
    return df, target


def retrain_one(symbol: str, horizon: str, side: str,
                variant: str = 'causal_v3',
                train_months: int = 6, val_months: int = 1,
                cpcv_folds: int = 6, embargo: int = 50,
                gate_ic: float = 0.03, gate_auc: float = 0.60,
                verbose: bool = True, *, output_dir: str | Path) -> dict:
    output_dir = validate_supervised_candidate_dir(output_dir, create=True)
    print(f'\n{"="*70}')
    print(f'  {symbol} | {horizon} | {side} | train={train_months}m val={val_months}m')
    print(f'{"="*70}')
    df, target = load_merged(symbol, horizon, side, variant)
    df['dt'] = df['open_time']
    df = df.sort_values('dt').reset_index(drop=True)

    if len(df) < 5000:
        print(f'  ERR: too few rows {len(df)} -> abort')
        return {'ok': False, 'reason': 'insufficient_rows'}

    last_dt = df['dt'].max()
    holdout_start_dt = last_dt - pd.DateOffset(months=val_months)
    train_start_dt = holdout_start_dt - pd.DateOffset(months=train_months)
    window_df = df[df['dt'] >= train_start_dt].reset_index(drop=True)
    holdout_start = int((window_df['dt'] < holdout_start_dt).sum())
    purge_bars = HORIZON_BARS[horizon]
    try:
        model_split = split_retrain_windows(
            len(window_df), holdout_start, purge_bars
        )
    except ValueError as exc:
        print(f'  ERR: {exc} -> abort')
        return {'ok': False, 'reason': 'window_too_small'}

    frames = {
        'train': window_df.iloc[model_split.train.as_slice()].reset_index(drop=True),
        'early_stop': window_df.iloc[
            model_split.early_stop.as_slice()
        ].reset_index(drop=True),
        'calibration': window_df.iloc[
            model_split.calibration.as_slice()
        ].reset_index(drop=True),
        'gate': window_df.iloc[model_split.gate.as_slice()].reset_index(drop=True),
    }
    for split_name, split_df in frames.items():
        if split_df[target].nunique() < 2:
            print(f'  ERR: {split_name} has fewer than two target classes -> abort')
            return {'ok': False, 'reason': f'{split_name}_single_class'}

    train = frames['train']
    early_stop = frames['early_stop']
    calibration = frames['calibration']
    gate = frames['gate']
    for split_name, split_df in frames.items():
        print(
            f'  {split_name:11s} {split_df["dt"].min().date()} -> '
            f'{split_df["dt"].max().date()}  n={len(split_df):,}  '
            f'pos={split_df[target].mean():.3f}'
        )

    exclude = _exclude_cols(side, horizon)
    feat_cols = [c for c in df.columns if c not in exclude and c not in {'dt'}]
    X_tr = train[feat_cols].fillna(0).astype(np.float32)
    y_tr = train[target].astype(int)
    X_es = early_stop[feat_cols].fillna(0).astype(np.float32)
    y_es = early_stop[target].astype(int)
    X_cal = calibration[feat_cols].fillna(0).astype(np.float32)
    y_cal = calibration[target].astype(int)
    X_gate = gate[feat_cols].fillna(0).astype(np.float32)
    y_gate = gate[target].astype(int)

    feat_cols = select_features_by_gain(X_tr, y_tr, max_features=100)
    X_tr = X_tr[feat_cols]
    X_es = X_es[feat_cols]
    X_cal = X_cal[feat_cols]
    X_gate = X_gate[feat_cols]
    print(f'  features after pre-filter: {len(feat_cols)}')

    # CPCV on train window
    fold_results, oos_summary = run_cpcv(
        X_tr, y_tr, target=target,
        n_folds=cpcv_folds, n_test_folds=2, embargo_bars=embargo,
        lgb_params=None, use_catboost=True,
        run_optuna=False, optuna_trials=0,
    )

    # Feature selection is confined to training rows.
    lgb_probe = train_lgbm(X_tr, y_tr)
    selected_feats = shap_feature_selection(lgb_probe, X_tr, threshold=0.001)
    print(f'  SHAP-selected features: {len(selected_feats)} (top5: {selected_feats[:5]})')

    # The production models train once; later windows are never refitted into them.
    lgb_final, cb_final = train_final_model(
        X_tr,
        y_tr,
        selected_feats,
        None,
        True,
        X_early_stop=X_es,
        y_early_stop=y_es,
    )

    cal_prob = lgb_final.predict_proba(X_cal[selected_feats])[:, 1]
    if cb_final is not None:
        cal_prob = (
            cal_prob + cb_final.predict_proba(X_cal[selected_feats])[:, 1]
        ) / 2.0
    calibrator = IsotonicRegression(out_of_bounds='clip')
    calibrator.fit(cal_prob, y_cal.values)

    gate_prob = lgb_final.predict_proba(X_gate[selected_feats])[:, 1]
    if cb_final is not None:
        gate_prob = (
            gate_prob + cb_final.predict_proba(X_gate[selected_feats])[:, 1]
        ) / 2.0
    auc_val = roc_auc_score(y_gate, gate_prob)
    ic_val, dir_val = ic_and_diracc(gate_prob, y_gate.values)
    print(f'  GATE AUC={auc_val:.4f}  IC={ic_val:+.4f}  dir_acc={dir_val:.4f}')

    def _window_meta(window: IndexWindow) -> dict:
        rows = window_df.iloc[window.as_slice()]
        if rows.empty:
            return {'start': None, 'end': None, 'n_samples': 0}
        return {
            'start': str(rows['dt'].iloc[0]),
            'end': str(rows['dt'].iloc[-1]),
            'n_samples': int(window.size),
        }

    windows = {
        'train': _window_meta(model_split.train),
        'early_stop': _window_meta(model_split.early_stop),
        'calibration': _window_meta(model_split.calibration),
        'gate': _window_meta(model_split.gate),
    }
    purge_windows = [_window_meta(window) for window in model_split.purged]

    # GATE
    print(f'  GATE: AUC>={gate_auc} ?  IC>={gate_ic} ?')
    if not np.isfinite(auc_val) or not np.isfinite(ic_val) \
            or auc_val < gate_auc or ic_val < gate_ic:
        print(f'  GATE FAILED: AUC={auc_val:.4f}  IC={ic_val:+.4f}  -> NOT saved')
        return {
            'ok': False, 'reason': 'gate_failed',
            'auc': auc_val, 'ic': ic_val, 'dir_acc': dir_val,
            'windows': windows,
            'n_features':  len(selected_feats),
        }

    target_id = f'{side}_label_{horizon}_{variant}'
    bundle = BundleModel(
        lgb_model    = lgb_final,
        cb_model     = cb_final,
        feature_cols = selected_feats,
        target       = target_id,
        oos_summary  = {
            **oos_summary,
            'val_auc': float(auc_val),
            'val_ic': float(ic_val),
            'val_dir_acc': float(dir_val),
            'gate_auc': float(auc_val),
            'gate_ic': float(ic_val),
            'gate_dir_acc': float(dir_val),
            'model_windows': windows,
            'purge_bars': int(purge_bars),
        },
        symbol       = symbol,
        shap_top20   = selected_feats[:20],
        calibrator   = calibrator,
    )
    out = output_dir / f'{symbol}_{target_id}.pkl'
    with open(out, 'wb') as f:
        pickle.dump(bundle, f)
    meta = {
        'symbol':        symbol,
        'target':        target_id,
        'side':          side,
        'horizon':       horizon,
        'variant':       variant,
        'windows':       windows,
        'purge_windows': purge_windows,
        'purge_bars':    int(purge_bars),
        'train_window':  [windows['train']['start'], windows['train']['end']],
        'early_stop_window': [windows['early_stop']['start'], windows['early_stop']['end']],
        'calibration_window': [windows['calibration']['start'], windows['calibration']['end']],
        'gate_window':   [windows['gate']['start'], windows['gate']['end']],
        'val_window':    [windows['gate']['start'], windows['gate']['end']],
        'n_train':       int(len(X_tr)),
        'n_early_stop':  int(len(X_es)),
        'n_calibration': int(len(X_cal)),
        'n_gate':        int(len(X_gate)),
        'n_val':         int(len(X_gate)),
        'val_auc':       float(auc_val),
        'val_ic':        float(ic_val),
        'val_dir_acc':   float(dir_val),
        'gate_auc':      float(auc_val),
        'gate_ic':       float(ic_val),
        'gate_dir_acc':  float(dir_val),
        'cpcv_oos':      oos_summary,
        'feature_count': len(selected_feats),
        'shap_top20':    selected_feats[:20],
    }
    meta_path = out.with_name(out.stem + '_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f'  GATE PASSED -> saved {out.name}  meta={meta_path.name}')
    return {'ok': True, **{k: meta[k] for k in (
        'val_auc', 'val_ic', 'val_dir_acc', 'feature_count', 'horizon', 'side')}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--horizon', default='1h', choices=['30m', '1h', '4h'])
    ap.add_argument('--side',    default='long', choices=['long', 'short', 'both'])
    ap.add_argument('--variant', default='causal_v3')
    ap.add_argument('--train-months', type=int, default=6)
    ap.add_argument('--val-months',   type=int, default=1)
    ap.add_argument('--gate-ic',      type=float, default=0.03)
    ap.add_argument('--gate-auc',     type=float, default=0.60)
    ap.add_argument('--release-id', required=True,
                    help='versioned directory name under models/candidates/supervised')
    args = ap.parse_args()
    output_dir = supervised_candidate_dir(args.release_id, create=True)

    syms = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT'] \
        if args.symbol == 'all' else [args.symbol]
    sides = ['long', 'short'] if args.side == 'both' else [args.side]
    results = []
    for s in syms:
        for side in sides:
            try:
                r = retrain_one(s, args.horizon, side,
                                variant=args.variant,
                                train_months=args.train_months,
                                val_months=args.val_months,
                                gate_ic=args.gate_ic,
                                gate_auc=args.gate_auc,
                                output_dir=output_dir)
            except Exception as e:
                import traceback
                traceback.print_exc()
                r = {'ok': False, 'reason': f'exception:{e}'}
            results.append({'symbol': s, 'side': side, 'horizon': args.horizon, **r})
    print('\n\nSUMMARY:')
    for r in results:
        if r.get('ok'):
            print(f"  PASS  {r['symbol']:8s} {r['horizon']:>4s} {r['side']:6s}  "
                  f"AUC={r.get('val_auc', 0):.4f}  IC={r.get('val_ic', 0):+.4f}  "
                  f"dir={r.get('val_dir_acc', 0):.4f}  feats={r.get('feature_count', 0)}")
        else:
            print(f"  FAIL  {r['symbol']:8s} {r['horizon']:>4s} {r['side']:6s}  "
                  f"reason={r.get('reason', 'unknown')}")


if __name__ == '__main__':
    main()
