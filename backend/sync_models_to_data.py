"""
把训练好的模型文件 + PSI 参考统计从 G 盘同步到 ./data/
让 Docker 容器不挂 G 盘也能使用 ML 信号。

使用方法（模型训练完成后运行一次）：
  python sync_models_to_data.py
"""
import sys, os, shutil
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, r'E:\File\Projects\binance\backend')
os.chdir(r'E:\File\Projects\binance\backend')

from app.datastore import MARKET_ROOT

SRC_MODELS   = MARKET_ROOT / 'models'
SRC_FEAT_ML  = MARKET_ROOT / 'features_ml'
DST_ROOT     = r'E:\File\Projects\binance\data'

def sync_dir(src, dst_root, subdir, pattern=None):
    dst = os.path.join(dst_root, subdir)
    os.makedirs(dst, exist_ok=True)
    copied = 0
    for f in os.listdir(src):
        if pattern and not any(f.endswith(p) for p in pattern):
            continue
        s = os.path.join(str(src), f)
        d = os.path.join(dst, f)
        shutil.copy2(s, d)
        copied += 1
    print(f'  {subdir}: {copied} 个文件 → {dst}')
    return copied

print('同步模型文件到 ./data/ ...')
sync_dir(SRC_MODELS, DST_ROOT, 'models', ['.pkl', '.json'])

print('同步特征参考统计 ...')
sync_dir(SRC_FEAT_ML, DST_ROOT, 'features_ml', ['_ref_stats.parquet'])

print('同步阈值文件 ...')
thresh = SRC_MODELS / 'thresholds.json'
if thresh.exists():
    shutil.copy2(str(thresh), os.path.join(DST_ROOT, 'models', 'thresholds.json'))
    print('  thresholds.json 已同步')

print('\n完成。重建 Docker 镜像后新模型即生效：')
print('  docker compose build backend && docker compose up -d backend')
