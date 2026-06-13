"""
优化版重训练：
  - 训练窗口：2023-01-01 ~ 2024-06-30（近 18 个月，去掉 2022 熊市噪声）
  - 验证窗口：2024-07-01 ~ 2024-12-31（6 个月，用于阈值校准）
  - 真 OOS：  2025-01-01 起（未参与任何调参）
  - Optuna：  30 trials 自动搜超参
  - SHAP：    threshold=0.003（更激进剪枝）
  - 训练后在验证集上扫 entry_threshold，找最优入场点
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os, time, subprocess, json, textwrap
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BACKEND    = r'E:\File\Projects\binance\backend'
SYMBOLS    = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT']
TRAIN_START = '2023-01-01'
TRAIN_END   = '2024-06-30'
VAL_START   = '2024-07-01'
VAL_END     = '2024-12-31'

# ── subprocess worker ──────────────────────────────────────────────
WORKER = textwrap.dedent(r"""
import warnings; warnings.filterwarnings('ignore')
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, r'E:\File\Projects\binance\backend')
os.chdir(r'E:\File\Projects\binance\backend')

sym         = sys.argv[1]
target      = sys.argv[2]
train_start = sys.argv[3]
train_end   = sys.argv[4]

from app.services.trend_predictor import train_symbol
r = train_symbol(
    sym, target=target,
    train_start=train_start, train_end=train_end,
    n_folds=5, n_test_folds=2, embargo_bars=50,
    use_catboost=True,
    run_optuna=True, optuna_trials=30,
    shap_threshold=0.003,
)
oos = r.get('oos_summary', {})
print(json.dumps({'symbol': sym, 'target': target,
                  'n_features': r.get('n_features', 0), **oos}))
""")

WORKER_PATH = os.path.join(BACKEND, '_retrain_worker.py')
with open(WORKER_PATH, 'w', encoding='utf-8') as f:
    f.write(WORKER)

# ── 阈值校准 worker ────────────────────────────────────────────────
CALIB_WORKER = textwrap.dedent(r"""
import warnings; warnings.filterwarnings('ignore')
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, r'E:\File\Projects\binance\backend')
os.chdir(r'E:\File\Projects\binance\backend')

sym       = sys.argv[1]
val_start = sys.argv[2]
val_end   = sys.argv[3]

from app.services.ml_strategy import MLTrendParams, backtest_ml_trend

# 扫 entry_threshold，其余用 thresholds.json 默认值
best = {'entry': 0.55, 'rev': 0.62, 'sharpe': -999}
for et in [0.54, 0.55, 0.56, 0.57, 0.58, 0.59, 0.60, 0.61, 0.62]:
    for rt in [0.60, 0.62, 0.64, 0.65, 0.67]:
        try:
            p = MLTrendParams.from_thresholds(sym, reversal_threshold=rt)
            p.entry_long_threshold  = et
            p.entry_short_threshold = et
            r = backtest_ml_trend(sym, p, val_start, val_end, verbose=False)
            m = r.get('metrics', {})
            sh = m.get('sharpe', -999)
            nt = m.get('n_trades', 0)
            if sh > best['sharpe'] and nt >= 5:
                best = {'entry': et, 'rev': rt, 'sharpe': sh,
                        'total_r': m.get('total_r', 0),
                        'win_rate': m.get('win_rate', 0),
                        'n_trades': nt}
        except Exception:
            pass

print(json.dumps({'symbol': sym, 'best_threshold': best}))
""")

CALIB_PATH = os.path.join(BACKEND, '_calib_worker.py')
with open(CALIB_PATH, 'w', encoding='utf-8') as f:
    f.write(CALIB_WORKER)

# ── 主流程 ─────────────────────────────────────────────────────────
print('=' * 65)
print(f'  优化重训练: {TRAIN_START} ~ {TRAIN_END}')
print(f'  验证集:    {VAL_START} ~ {VAL_END}  (阈值校准)')
print(f'  真 OOS:   2025-01-01 ~  (未触碰)')
print(f'  Optuna:   30 trials/模型    SHAP: threshold=0.003')
print(f'  币种: {SYMBOLS}  (subprocess隔离)')
print('=' * 65)

t_total = time.time()
results = []

# Phase 1: 训练
print('\n── Phase 1: 训练 ──────────────────────────────────')
for sym in SYMBOLS:
    for target in ['long_label', 'short_label']:
        t0 = time.time()
        print(f'\n[{sym}] 目标={target}')
        print('=' * 60)
        proc = subprocess.run(
            [sys.executable, WORKER_PATH, sym, target, TRAIN_START, TRAIN_END],
            capture_output=True, errors='ignore', timeout=3600
        )
        elapsed = time.time() - t0
        stderr = proc.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[-5:]:
                print(f'  WARN: {line}')
        if proc.returncode != 0:
            print(f'  ERROR {sym} {target}: exit={proc.returncode}  耗时={elapsed:.0f}s')
            continue
        json_line = ''
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.strip().startswith('{'):
                json_line = line.strip(); break
        try:
            row = json.loads(json_line)
            print(f'  OK  {sym} {target}: '
                  f'AUC={row.get("auc",0):.4f}  '
                  f'n_features={row.get("n_features",0)}  '
                  f'耗时={elapsed:.0f}s')
            results.append(row)
        except Exception as e:
            print(f'  PARSE ERR: {e}  raw={json_line!r}')

# Phase 2: 阈值校准
print('\n── Phase 2: 阈值校准 (2024-H2 验证集) ─────────────')
thresholds = {}
for sym in SYMBOLS:
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, CALIB_PATH, sym, VAL_START, VAL_END],
        capture_output=True, errors='ignore', timeout=600
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(f'  {sym}: 校准失败 (exit={proc.returncode})')
        continue
    json_line = ''
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith('{'):
            json_line = line.strip(); break
    try:
        row = json.loads(json_line)
        best = row.get('best_threshold', {})
        thresholds[sym] = best
        print(f'  {sym}: entry={best.get("entry",0):.2f}  rev={best.get("rev",0):.2f}'
              f'  sharpe={best.get("sharpe",0):.3f}  n={best.get("n_trades",0)}'
              f'  耗时={elapsed:.0f}s')
    except Exception as e:
        print(f'  {sym}: parse err {e}')

# 将校准结果写入 thresholds_optimized.json
thresholds_path = os.path.join(BACKEND, 'data', 'thresholds_optimized.json')
with open(thresholds_path, 'w', encoding='utf-8') as f:
    json.dump(thresholds, f, indent=2, ensure_ascii=False)
print(f'\n  校准阈值已保存 → {thresholds_path}')

print(f'\n总耗时: {(time.time()-t_total)/60:.1f} 分钟')
print('优化训练完成。模型已覆盖存入 models/')

try:
    os.remove(WORKER_PATH)
    os.remove(CALIB_PATH)
except Exception:
    pass
