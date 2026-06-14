"""
Phase 4 — 趋势预测模型（深度版）

模型栈：
  LightGBM（基线，快速，Optuna 调参）
  CatBoost（处理类别/非线性，与 LGB 互补）
  集成层（等权平均）

验证框架：
  CPCV — Combinatorial Purged Cross-Validation（López de Prado）
  n_folds=6, n_test_folds=2 → C(6,2)=15 个独立 OOS 片段
  embargo_bars=50 (5m×50≈4h) 防泄漏

特征选择：
  SHAP Mean Absolute Value 剪枝，去掉贡献 < 0.1% 的特征

超参优化：
  Optuna TPE，早停防过拟合

输出：
  BundleModel 包含 lgb/cb 模型 + 特征列表 + CPCV OOS 指标
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import json, pickle, warnings
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any
from itertools import combinations

warnings.filterwarnings('ignore')


# ══════════════════════════════════════════════════════════════════════════════
# CPCV — Combinatorial Purged Cross-Validation
# ══════════════════════════════════════════════════════════════════════════════

class CPCVSplitter:
    """
    López de Prado (2018) 组合净化交叉验证。

    参数
    ----
    n_folds      : 总折数，默认 6
    n_test_folds : 每次测试使用的折数，默认 2
    embargo_bars : 训练集末尾与测试集首行之间的净化间隔（bar 数）
    """

    def __init__(self, n_folds: int = 6, n_test_folds: int = 2,
                 embargo_bars: int = 50):
        self.n_folds      = n_folds
        self.n_test_folds = n_test_folds
        self.embargo_bars = embargo_bars

    def split(self, X: pd.DataFrame) -> List[Tuple[np.ndarray, np.ndarray]]:
        n = len(X)
        fold_size = n // self.n_folds
        boundaries = [i * fold_size for i in range(self.n_folds)] + [n]
        folds = [list(range(boundaries[i], boundaries[i + 1]))
                 for i in range(self.n_folds)]

        splits = []
        for test_fold_ids in combinations(range(self.n_folds), self.n_test_folds):
            test_idx = []
            for fi in test_fold_ids:
                test_idx.extend(folds[fi])

            train_idx_raw = []
            for fi in range(self.n_folds):
                if fi not in test_fold_ids:
                    train_idx_raw.extend(folds[fi])

            # embargo：测试集两侧各净化 embargo_bars 个训练样本
            # 右侧同样需要净化：标注窗口 48bar，右侧无缓冲会导致训练集延伸进入测试集
            test_min = min(test_idx)
            test_max = max(test_idx)
            train_idx = [i for i in train_idx_raw
                         if i < test_min - self.embargo_bars
                         or i > test_max + self.embargo_bars]

            if len(train_idx) < 1000 or len(test_idx) < 100:
                continue

            splits.append((np.array(sorted(train_idx)),
                            np.array(sorted(test_idx))))
        return splits


# ══════════════════════════════════════════════════════════════════════════════
# 特征选择 — SHAP 剪枝
# ══════════════════════════════════════════════════════════════════════════════

def _pre_filter_features(
    X:            pd.DataFrame,
    feat_cols:    List[str],
    max_features: int = 200,
    corr_thresh:  float = 0.97,
) -> List[str]:
    """
    CPCV 前快速预筛特征：
    1) 去零方差列
    2) 高相关去重（保留方差大的）
    3) 按方差排序保留 top max_features
    """
    if len(feat_cols) <= max_features:
        return feat_cols

    Xf = X[feat_cols]

    # 1) 去零方差，按方差降序（贪心去重时保留高方差特征）
    var = Xf.var().sort_values(ascending=False)
    feat_cols = [c for c in var.index if var[c] > 1e-12]
    Xf = Xf[feat_cols]

    if len(feat_cols) <= max_features:
        return feat_cols

    # 2) 高相关去重（np.corrcoef 用 BLAS，比 pd.corr() 快 100x）
    n_sample = min(10_000, len(Xf))
    Xs = Xf.sample(n_sample, random_state=42).values.astype(np.float32)
    # 去均值 + 标准化，避免 nan
    Xs -= Xs.mean(axis=0, keepdims=True)
    std = Xs.std(axis=0, keepdims=True)
    std[std < 1e-8] = 1.0
    Xs /= std
    corr_arr = (Xs.T @ Xs) / n_sample        # shape (n_feat, n_feat)
    corr_arr = np.abs(corr_arr)
    np.fill_diagonal(corr_arr, 0.0)
    # 贪心去高相关：优先保留方差大的（feat_cols 已按 var 降序）
    drop_mask = np.zeros(len(feat_cols), dtype=bool)
    for i in range(len(feat_cols)):
        if drop_mask[i]:
            continue
        drop_mask[i + 1:] |= corr_arr[i, i + 1:] > corr_thresh
    feat_cols = [c for c, d in zip(feat_cols, drop_mask) if not d]
    Xf = Xf[feat_cols]

    if len(feat_cols) <= max_features:
        return feat_cols

    # 3) 按方差降序取 top max_features
    var2 = Xf.var().sort_values(ascending=False)
    feat_cols = list(var2.index[:max_features])
    return feat_cols


def shap_feature_selection(
    model,
    X:          pd.DataFrame,
    threshold:  float = 0.01,   # 提高到1%：实质性去除噪声特征（原0.1%过低）
    max_feats:  int   = 150,
    sample_n:   int   = 5000,
) -> List[str]:
    """基于 SHAP Mean |值| 剪枝，按重要性降序保留 top max_feats。"""
    try:
        import shap
    except ImportError:
        print('  WARN: shap 未安装，跳过特征剪枝，使用全部特征')
        return list(X.columns)

    sample = X.sample(min(sample_n, len(X)), random_state=42)
    explainer = shap.TreeExplainer(model)
    sv = explainer.shap_values(sample)
    if isinstance(sv, list):
        sv = sv[1]

    importance = np.abs(sv).mean(axis=0)
    max_imp    = importance.max()
    keep_mask  = importance >= threshold * max_imp
    # 按 SHAP 重要性降序排列后截断，保证 max_feats 内都是信息最丰富的特征
    kept_pairs = sorted(
        [(c, importance[i]) for i, (c, k) in enumerate(zip(X.columns, keep_mask)) if k],
        key=lambda x: x[1], reverse=True
    )
    kept = [c for c, _ in kept_pairs[:max_feats]]
    print(f'  SHAP 剪枝: {len(X.columns)} → {len(kept)} 个特征 '
          f'(threshold={threshold}, top={max_feats})')
    return kept


# ══════════════════════════════════════════════════════════════════════════════
# LightGBM 训练
# ══════════════════════════════════════════════════════════════════════════════

def _lgb_device() -> str:
    """检测是否有可用 GPU，有则返回 'gpu'，否则 'cpu'"""
    try:
        import lightgbm as lgb
        import numpy as np
        # 小数据集快速探针
        ds = lgb.Dataset(np.random.rand(100, 5), label=np.random.randint(0, 2, 100))
        lgb.train({'device': 'gpu', 'verbose': -1, 'num_leaves': 4}, ds,
                  num_boost_round=2)
        print('  LightGBM: 使用 GPU 训练')
        return 'gpu'
    except Exception:
        return 'cpu'

_LGB_DEVICE = None   # 懒加载，首次训练时检测


_LGB_DEFAULTS = dict(
    n_estimators      = 800,
    learning_rate     = 0.05,
    num_leaves        = 63,
    max_depth         = -1,
    min_child_samples = 50,
    subsample         = 0.8,
    colsample_bytree  = 0.7,
    reg_alpha         = 0.1,
    reg_lambda        = 0.2,
    class_weight      = 'balanced',
    random_state      = 42,
    n_jobs            = 4,
    verbose           = -1,
)

# 轻量版：CPCV 折内仅用于 AUC 评估，不需要最优精度
_LGB_CPCV_PARAMS = dict(
    n_estimators      = 300,
    learning_rate     = 0.1,
    num_leaves        = 31,
    max_depth         = 6,
    min_child_samples = 100,
    subsample         = 0.8,
    colsample_bytree  = 0.7,
    reg_alpha         = 0.1,
    reg_lambda        = 0.2,
    class_weight      = 'balanced',
    random_state      = 42,
    n_jobs            = 4,
    verbose           = -1,
)


def train_lgbm(
    X_tr: pd.DataFrame, y_tr: pd.Series,
    X_va: Optional[pd.DataFrame] = None,
    y_va: Optional[pd.Series]    = None,
    params: Optional[dict]       = None,
):
    from lightgbm import LGBMClassifier
    global _LGB_DEVICE
    if _LGB_DEVICE is None:
        _LGB_DEVICE = _lgb_device()

    p = {**_LGB_DEFAULTS, 'device': _LGB_DEVICE, **(params or {})}
    model = LGBMClassifier(**p)
    if X_va is not None and y_va is not None:
        model.fit(
            X_tr, y_tr,
            eval_set=[(X_va, y_va)],
            callbacks=[
                __import__('lightgbm').early_stopping(50, verbose=False),
                __import__('lightgbm').log_evaluation(0),
            ],
        )
    else:
        model.fit(X_tr, y_tr)
    return model


# ══════════════════════════════════════════════════════════════════════════════
# CatBoost 训练
# ══════════════════════════════════════════════════════════════════════════════

def _cb_task_type() -> str:
    """检测 CatBoost 是否可用 GPU"""
    try:
        from catboost import CatBoostClassifier
        import numpy as np
        m = CatBoostClassifier(iterations=2, task_type='GPU', verbose=0)
        m.fit(np.random.rand(100, 5), np.random.randint(0, 2, 100))
        print('  CatBoost: 使用 GPU 训练')
        return 'GPU'
    except Exception:
        return 'CPU'

_CB_TASK_TYPE = None   # 懒加载


_CB_DEFAULTS = dict(
    iterations         = 600,
    learning_rate      = 0.05,
    depth              = 6,
    l2_leaf_reg        = 3,
    random_seed        = 42,
    eval_metric        = 'AUC',
    auto_class_weights = 'Balanced',
    verbose            = 0,
)

# CPCV 折内轻量版：仅用于 AUC 估计，不追求最优精度
_CB_CPCV_PARAMS = dict(
    iterations         = 150,
    learning_rate      = 0.1,
    depth              = 4,
    l2_leaf_reg        = 3,
    random_seed        = 42,
    eval_metric        = 'Logloss',   # Logloss 比 AUC 快得多
    auto_class_weights = 'Balanced',
    verbose            = 0,
)


def train_catboost(
    X_tr: pd.DataFrame, y_tr: pd.Series,
    X_va: Optional[pd.DataFrame] = None,
    y_va: Optional[pd.Series]    = None,
    params: Optional[dict]       = None,
):
    global _CB_TASK_TYPE
    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        print('  WARN: catboost 未安装，跳过 CB 训练')
        return None
    if _CB_TASK_TYPE is None:
        _CB_TASK_TYPE = _cb_task_type()

    p = {**_CB_DEFAULTS, 'task_type': _CB_TASK_TYPE, **(params or {})}
    model = CatBoostClassifier(**p)
    if X_va is not None and y_va is not None:
        tr_pool = Pool(X_tr, y_tr)
        va_pool = Pool(X_va, y_va)
        model.fit(tr_pool, eval_set=va_pool, early_stopping_rounds=50)
    else:
        model.fit(X_tr, y_tr)
    return model


# ══════════════════════════════════════════════════════════════════════════════
# Optuna 超参优化（LightGBM）
# ══════════════════════════════════════════════════════════════════════════════

def optimize_lgbm(
    X: pd.DataFrame,
    y: pd.Series,
    n_trials:     int = 50,
    cv_splits:    int = 3,
    embargo_bars: int = 50,
) -> dict:
    """Optuna TPE 对 LightGBM 超参优化，返回最优参数 dict。"""
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print('  WARN: optuna 未安装，使用默认超参')
        return {}

    from sklearn.metrics import roc_auc_score
    from lightgbm import LGBMClassifier, early_stopping, log_evaluation

    splitter = CPCVSplitter(n_folds=cv_splits + 1, n_test_folds=1,
                            embargo_bars=embargo_bars)
    splits = splitter.split(X)[:cv_splits]

    def objective(trial):
        p = dict(
            n_estimators      = trial.suggest_int('n_estimators', 300, 1200),
            learning_rate     = trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
            num_leaves        = trial.suggest_int('num_leaves', 31, 127),
            min_child_samples = trial.suggest_int('min_child_samples', 20, 100),
            subsample         = trial.suggest_float('subsample', 0.6, 1.0),
            colsample_bytree  = trial.suggest_float('colsample_bytree', 0.5, 1.0),
            reg_alpha         = trial.suggest_float('reg_alpha', 1e-3, 1.0, log=True),
            reg_lambda        = trial.suggest_float('reg_lambda', 1e-3, 1.0, log=True),
            class_weight      = 'balanced',
            random_state      = 42,
            n_jobs            = -1,
            verbose           = -1,
        )
        aucs = []
        for tr_idx, va_idx in splits:
            X_tr, y_tr = X.iloc[tr_idx], y.iloc[tr_idx]
            X_va, y_va = X.iloc[va_idx], y.iloc[va_idx]
            if y_va.sum() < 5:
                continue
            m = LGBMClassifier(**p)
            m.fit(
                X_tr, y_tr,
                eval_set=[(X_va, y_va)],
                callbacks=[early_stopping(30, verbose=False), log_evaluation(0)],
            )
            prob = m.predict_proba(X_va)[:, 1]
            aucs.append(roc_auc_score(y_va, prob))
        return float(np.mean(aucs)) if aucs else 0.5

    study = optuna.create_study(direction='maximize',
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    best = study.best_params
    print(f'  Optuna 最优 AUC={study.best_value:.4f}  n_estimators={best["n_estimators"]}')
    return best


# ══════════════════════════════════════════════════════════════════════════════
# 评估指标
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_predictions(y_true: np.ndarray, y_prob: np.ndarray) -> dict:
    from sklearn.metrics import (
        roc_auc_score, average_precision_score,
        precision_score, recall_score, f1_score,
    )
    if y_true.sum() < 2:
        return {}

    thresh = 0.5
    y_pred = (y_prob >= thresh).astype(int)
    return {
        'auc':       round(float(roc_auc_score(y_true, y_prob)),           4),
        'ap':        round(float(average_precision_score(y_true, y_prob)), 4),
        'precision': round(float(precision_score(y_true, y_pred, zero_division=0)), 4),
        'recall':    round(float(recall_score(y_true, y_pred, zero_division=0)),    4),
        'f1':        round(float(f1_score(y_true, y_pred, zero_division=0)),        4),
        'pos_rate':  round(float(y_true.mean()), 4),
        'n_samples': int(len(y_true)),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CPCV 主流程
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FoldResult:
    fold_id:  int
    metrics:  dict
    n_train:  int
    n_test:   int
    y_prob:   np.ndarray = field(repr=False)
    y_true:   np.ndarray = field(repr=False)


def run_cpcv(
    X:             pd.DataFrame,
    y:             pd.Series,
    target:        str   = 'long_label',
    n_folds:       int   = 6,
    n_test_folds:  int   = 2,
    embargo_bars:  int   = 50,
    lgb_params:    Optional[dict] = None,
    use_catboost:  bool  = True,
    run_optuna:    bool  = False,
    optuna_trials: int   = 30,
) -> Tuple[List[FoldResult], dict]:
    """在 X/y 上跑完整 CPCV，返回每折结果和汇总统计。"""
    splitter = CPCVSplitter(n_folds, n_test_folds, embargo_bars)
    splits   = splitter.split(X)
    print(f'  CPCV: {len(splits)} 个 OOS 片段  (C({n_folds},{n_test_folds})={len(splits)})', flush=True)

    if run_optuna and splits:
        print('  运行 Optuna 调参...')
        X_sub = X.iloc[splits[0][0]]
        y_sub = y.iloc[splits[0][0]]
        lgb_params = optimize_lgbm(X_sub, y_sub, n_trials=optuna_trials)

    import gc
    fold_results = []
    all_prob, all_true = [], []
    # CPCV 折内用轻量参数：仅用于 AUC 评估，不追求最优精度
    cpcv_lgb_params = lgb_params or _LGB_CPCV_PARAMS
    cpcv_cb_params  = _CB_CPCV_PARAMS

    for fold_id, (tr_idx, va_idx) in enumerate(splits):
        X_tr, y_tr = X.iloc[tr_idx].copy(), y.iloc[tr_idx]
        X_va, y_va = X.iloc[va_idx].copy(), y.iloc[va_idx]

        if y_va.sum() < 5:
            continue

        # 分一小部分训练集做 early-stopping 验证
        n_es  = max(int(len(X_tr) * 0.1), 200)
        X_es  = X_tr.iloc[-n_es:]
        y_es  = y_tr.iloc[-n_es:]
        X_tr2 = X_tr.iloc[:-n_es]
        y_tr2 = y_tr.iloc[:-n_es]

        lgb_m = train_lgbm(X_tr2, y_tr2, X_es, y_es, cpcv_lgb_params)
        prob_lgb = lgb_m.predict_proba(X_va)[:, 1]

        if use_catboost:
            cb_m = train_catboost(X_tr2, y_tr2, X_es, y_es, cpcv_cb_params)
            if cb_m is not None:
                prob_cb = cb_m.predict_proba(X_va)[:, 1]
                prob = (prob_lgb + prob_cb) / 2
                del cb_m
            else:
                prob = prob_lgb
        else:
            prob = prob_lgb

        metrics = evaluate_predictions(y_va.values, prob)
        fold_results.append(FoldResult(
            fold_id=fold_id, metrics=metrics,
            n_train=len(tr_idx), n_test=len(va_idx),
            y_prob=prob, y_true=y_va.values,
        ))
        all_prob.extend(prob)
        all_true.extend(y_va.values)
        print(f'  Fold {fold_id:02d}: AUC={metrics.get("auc",0):.4f}  '
              f'AP={metrics.get("ap",0):.4f}  '
              f'n_test={len(va_idx):,}', flush=True)
        del lgb_m, X_tr, X_va, X_tr2, X_es
        gc.collect()

    oos = evaluate_predictions(np.array(all_true), np.array(all_prob))
    oos['n_folds'] = len(fold_results)
    per_fold_aucs  = [r.metrics.get('auc', 0) for r in fold_results]
    oos['auc_std'] = round(float(np.std(per_fold_aucs)), 4) if per_fold_aucs else 0

    print(f'\n  OOS 汇总 AUC={oos.get("auc",0):.4f}±{oos["auc_std"]:.4f}  '
          f'AP={oos.get("ap",0):.4f}  n_folds={len(fold_results)}', flush=True)
    return fold_results, oos


# ══════════════════════════════════════════════════════════════════════════════
# 最终模型（全量数据训练）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BundleModel:
    """保存 LGB + CB 模型 + 特征列表 + OOS 指标"""
    lgb_model:    Any
    cb_model:     Any
    feature_cols: List[str]
    target:       str
    oos_summary:  dict
    symbol:       str
    shap_top20:   Optional[List[str]] = None
    calibrator:   Optional[Any] = None   # Isotonic regression calibrator

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        X_f = X[self.feature_cols].copy()
        p_lgb = self.lgb_model.predict_proba(X_f)[:, 1]
        if self.cb_model is not None:
            p_cb = self.cb_model.predict_proba(X_f)[:, 1]
            raw = (p_lgb + p_cb) / 2
        else:
            raw = p_lgb
        if self.calibrator is not None:
            return self.calibrator.transform(raw)
        return raw


def train_final_model(
    X:            pd.DataFrame,
    y:            pd.Series,
    feature_cols: List[str],
    lgb_params:   Optional[dict] = None,
    use_catboost: bool  = True,
) -> Tuple[Any, Any]:
    """用全量数据训练最终模型（留最后 10% 做 early-stopping）"""
    X_f   = X[feature_cols]
    n_val = max(int(len(X_f) * 0.1), 500)
    X_tr, y_tr = X_f.iloc[:-n_val], y.iloc[:-n_val]
    X_va, y_va = X_f.iloc[-n_val:], y.iloc[-n_val:]

    lgb = train_lgbm(X_tr, y_tr, X_va, y_va, lgb_params)
    cb  = None
    if use_catboost:
        cb = train_catboost(X_tr, y_tr, X_va, y_va)
    return lgb, cb


# ══════════════════════════════════════════════════════════════════════════════
# 持久化
# ══════════════════════════════════════════════════════════════════════════════

def _model_dir() -> Path:
    from ..datastore import MARKET_ROOT
    d = MARKET_ROOT / 'models'
    d.mkdir(exist_ok=True)
    return d


def save_model(bundle: BundleModel) -> Path:
    d    = _model_dir()
    path = d / f'{bundle.symbol}_{bundle.target}.pkl'
    with open(path, 'wb') as f:
        pickle.dump(bundle, f)
    meta_path = d / f'{bundle.symbol}_{bundle.target}_meta.json'
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({
            'symbol':        bundle.symbol,
            'target':        bundle.target,
            'oos_summary':   bundle.oos_summary,
            'feature_count': len(bundle.feature_cols),
            'shap_top20':    bundle.shap_top20,
        }, f, indent=2, ensure_ascii=False)
    print(f'  模型保存 → {path}')
    return path


def load_model(symbol: str, target: str) -> BundleModel:
    path = _model_dir() / f'{symbol}_{target}.pkl'
    if not path.exists():
        raise FileNotFoundError(f'模型未找到: {path}')
    with open(path, 'rb') as f:
        return pickle.load(f)


def predict_proba(bundle: BundleModel, feat_row: pd.Series) -> float:
    """对单行特征进行推断，返回正类概率"""
    X = pd.DataFrame([feat_row])[bundle.feature_cols]
    X = X.fillna(0).astype(np.float32)
    probs = bundle.predict_proba(X)
    return float(probs[0])


# ══════════════════════════════════════════════════════════════════════════════
# 端到端：单币训练
# ══════════════════════════════════════════════════════════════════════════════

def train_symbol(
    symbol:         str,
    target:         str   = 'long_label',
    n_folds:        int   = 6,
    n_test_folds:   int   = 2,
    embargo_bars:   int   = 50,
    use_catboost:   bool  = True,
    run_optuna:     bool  = False,
    optuna_trials:  int   = 30,
    shap_threshold: float = 0.001,
    train_end:      str   = None,   # 截止日期，如 '2024-12-31'；None = 全量
    train_start:    str   = None,   # 起始日期，如 '2023-01-01'；None = 全量
) -> dict:
    """
    加载 symbol 特征+标签 → CPCV 评估 → SHAP 剪枝 → 全量训练 → 保存模型
    train_end/train_start: 时间窗口切割，支持滚动窗口训练
    """
    from .feature_builder import merge_features_labels

    print(f'\n{"="*60}')
    print(f'[{symbol}] 目标={target}' + (f'  {train_start}~{train_end}' if train_end else ''))
    print(f'{"="*60}')

    df = merge_features_labels(symbol)
    ot = df['open_time']
    ts = ot if ot.dtype.kind == 'M' else pd.to_datetime(ot, unit='ms')
    if hasattr(ts.dt, 'tz') and ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)

    if train_start is not None:
        df = df[ts >= pd.Timestamp(train_start)].reset_index(drop=True)
        ts  = ts[ts >= pd.Timestamp(train_start)].reset_index(drop=True)
        print(f'  起始过滤后样本数={len(df):,}', flush=True)
    if train_end is not None:
        df = df[ts <= pd.Timestamp(train_end)].reset_index(drop=True)
        print(f'  截止过滤后样本数={len(df):,}', flush=True)

    if target not in df.columns:
        print(f'  ERR: 标注列 {target} 不存在')
        return {}

    exclude = {
        'open_time', 'year', 'index',   # 'index' = 行号列，PSI=12.43 时序泄漏
        'long_label',  'long_meta_label',  'long_profit_r',  'long_duration',
        'short_label', 'short_meta_label', 'short_profit_r', 'short_duration',
        'long_trend_sharpe', 'short_trend_sharpe', 'best_direction',
        'long_barrier_hit', 'short_barrier_hit',
        'long_trend_consistency', 'short_trend_consistency',
        'long_max_adverse_r', 'short_max_adverse_r',
        'long_max_favorable_r', 'short_max_favorable_r',
        'long_duration_bars', 'short_duration_bars',
        'atr',
    }
    feat_cols = [c for c in df.columns if c not in exclude]

    X = df[feat_cols].fillna(0).astype(np.float32)
    y = df[target].astype(int)

    pos_rate = y.mean()
    print(f'  样本数={len(X):,}  正例率={pos_rate:.3f}  特征数={len(feat_cols)}', flush=True)

    if pos_rate < 0.01 or pos_rate > 0.99:
        print(f'  WARN: 正例率极端 ({pos_rate:.3f})，可能标注有问题')

    # Step 0: CPCV 前快速预筛特征（降内存占用）
    feat_cols = _pre_filter_features(X, feat_cols, max_features=200)
    X = X[feat_cols]
    print(f'  预筛后特征数={len(feat_cols)}', flush=True)

    # Step 1: CPCV 评估
    fold_results, oos_summary = run_cpcv(
        X, y,
        target=target,
        n_folds=n_folds,
        n_test_folds=n_test_folds,
        embargo_bars=embargo_bars,
        use_catboost=use_catboost,
        run_optuna=run_optuna,
        optuna_trials=optuna_trials,
    )

    # Step 2: SHAP 特征剪枝（用第一折训练集）
    shap_top20    = None
    selected_feats = feat_cols
    if fold_results:
        first_tr_idx = CPCVSplitter(n_folds, n_test_folds, embargo_bars).split(X)[0][0]
        X_sub = X.iloc[first_tr_idx]
        y_sub = y.iloc[first_tr_idx]
        lgb_probe = train_lgbm(X_sub, y_sub)
        selected_feats = shap_feature_selection(lgb_probe, X_sub,
                                                threshold=shap_threshold)
        shap_top20 = selected_feats[:20]
        print(f'  SHAP TOP-20 样本: {shap_top20[:5]}...')

    # Step 3: 全量训练
    print('\n  训练最终模型（全量数据）...')
    lgb_best_params = None
    if run_optuna and fold_results:
        lgb_best_params = optimize_lgbm(X[selected_feats], y,
                                        n_trials=max(optuna_trials // 2, 10))

    lgb_final, cb_final = train_final_model(
        X, y, selected_feats, lgb_best_params, use_catboost
    )

    # Step 3b: Isotonic 概率校准（在最后 20% 时序片段上拟合）
    calibrator = None
    try:
        from sklearn.isotonic import IsotonicRegression
        cal_n = max(int(len(X) * 0.2), 500)
        X_cal = X[selected_feats].iloc[-cal_n:]
        y_cal = y.iloc[-cal_n:]
        cal_p = lgb_final.predict_proba(X_cal)[:, 1]
        if cb_final is not None:
            cal_p = (cal_p + cb_final.predict_proba(X_cal)[:, 1]) / 2
        iso = IsotonicRegression(out_of_bounds='clip')
        iso.fit(cal_p, y_cal.values)
        calibrator = iso
        print(f'  Isotonic 校准完成 n_cal={cal_n}', flush=True)
    except Exception as _e:
        print(f'  WARN: Isotonic 校准失败: {_e}', flush=True)

    # Step 4: 保存
    bundle = BundleModel(
        lgb_model    = lgb_final,
        cb_model     = cb_final,
        feature_cols = selected_feats,
        target       = target,
        oos_summary  = oos_summary,
        symbol       = symbol,
        shap_top20   = shap_top20,
        calibrator   = calibrator,
    )
    save_model(bundle)

    return {
        'symbol':      symbol,
        'target':      target,
        'oos_summary': oos_summary,
        'n_features':  len(selected_feats),
        'shap_top20':  shap_top20,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 模型诊断报告
# ══════════════════════════════════════════════════════════════════════════════

def model_diagnosis(bundle: BundleModel, X_test: pd.DataFrame,
                    y_test: pd.Series) -> dict:
    """OOS 诊断：概率校准、月度 AUC 时间稳定性、特征重要性"""
    from sklearn.metrics import roc_auc_score

    X_f   = X_test[bundle.feature_cols].fillna(0).astype(np.float32)
    probs = bundle.predict_proba(X_f)

    # 概率校准（10 分位）
    bins = pd.qcut(probs, q=10, duplicates='drop')
    cal  = pd.DataFrame({'prob': probs, 'actual': y_test.values, 'bin': bins})
    calibration = cal.groupby('bin', observed=True).agg(
        mean_pred=('prob', 'mean'),
        mean_actual=('actual', 'mean'),
        n=('actual', 'count'),
    ).reset_index(drop=True).to_dict('records')

    # 月度 AUC（时间稳定性）
    monthly = {}
    if 'open_time' in X_test.columns:
        ym = pd.to_datetime(X_test['open_time'], unit='ms').dt.to_period('M')
        for period in ym.unique():
            mask = (ym == period).values
            if mask.sum() < 50 or y_test.values[mask].sum() < 5:
                continue
            try:
                monthly[str(period)] = round(
                    float(roc_auc_score(y_test.values[mask], probs[mask])), 4
                )
            except Exception:
                pass

    # 特征重要性（LGB）
    lgb_imp = None
    if hasattr(bundle.lgb_model, 'feature_importances_'):
        imp = dict(zip(bundle.feature_cols,
                       bundle.lgb_model.feature_importances_.tolist()))
        lgb_imp = sorted(imp.items(), key=lambda x: -x[1])[:20]

    return {
        'oos_metrics':       evaluate_predictions(y_test.values, probs),
        'calibration':       calibration,
        'monthly_auc':       monthly,
        'feature_imp_top20': lgb_imp,
    }
