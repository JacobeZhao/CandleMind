"""
时间切割重训练：只用 2022-01-01 ~ 2024-12-31 的数据训练模型
2025-01-01 之后的数据作为纯 OOS holdout
每个 (symbol, target) 在独立子进程中运行，避免 OOM。
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os, time, subprocess, json, textwrap
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BACKEND = r'E:\File\Projects\binance\backend'
TRAIN_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT']
TRAIN_END     = '2024-12-31'

WORKER = textwrap.dedent(r"""
import warnings; warnings.filterwarnings('ignore')
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, r'E:\File\Projects\binance\backend')
os.chdir(r'E:\File\Projects\binance\backend')

sym    = sys.argv[1]
target = sys.argv[2]
end    = sys.argv[3]

from app.services.trend_predictor import train_symbol
r = train_symbol(sym, target=target, train_end=end,
                 n_folds=5, n_test_folds=2, embargo_bars=50,
                 use_catboost=True, run_optuna=False)
oos = r.get('oos_summary', {})
print(json.dumps({'symbol': sym, 'target': target, **oos}))
""")

WORKER_PATH = os.path.join(BACKEND, '_retrain_worker.py')
with open(WORKER_PATH, 'w', encoding='utf-8') as f:
    f.write(WORKER)

print('=' * 65)
print(f'  时间切割重训练: 2022-01-01 ~ {TRAIN_END}')
print(f'  OOS holdout:  2025-01-01 ~ 2026-06-04')
print(f'  币种: {TRAIN_SYMBOLS}  (subprocess隔离)')
print('=' * 65)

t_total = time.time()
results = []

for sym in TRAIN_SYMBOLS:
    for target in ['long_label', 'short_label']:
        t0 = time.time()
        print(f'\n[{sym}] 目标={target}  截止={TRAIN_END}')
        print('=' * 60)
        proc = subprocess.run(
            [sys.executable, WORKER_PATH, sym, target, TRAIN_END],
            capture_output=True, errors='ignore', timeout=1800
        )
        elapsed = time.time() - t0
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if stderr:
            for line in stderr.splitlines()[-10:]:
                print(f'  WARN: {line}')

        if proc.returncode != 0:
            print(f'  ERROR {sym} {target}: exit={proc.returncode}  耗时={elapsed:.0f}s')
            continue

        # last non-empty line should be the JSON result
        json_line = ''
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                json_line = line
                break

        try:
            row = json.loads(json_line)
            auc = row.get('auc', 0)
            acc = row.get('accuracy', 0)
            print(f'  OK  {sym} {target}: AUC={auc:.4f}  acc={acc:.4f}  耗时={elapsed:.0f}s')
            results.append(row)
        except Exception as e:
            print(f'  PARSE ERROR {sym} {target}: {e}  output={json_line!r}')

print(f'\n总耗时: {(time.time()-t_total)/60:.1f} 分钟')
print('\n训练完成，模型已覆盖存入 models/')
print('原始全量模型备份在 models_archive_full_2022_2026/')

try:
    os.remove(WORKER_PATH)
except Exception:
    pass
