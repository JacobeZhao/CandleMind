"""
统一数据存储路径。所有行情数据 + 回测数据集中存到 G:\\5、金融交易。
可用环境变量 MARKET_DATA_DIR 覆盖；该盘不可用时回退到 ./data（保证不崩）。
"""
import os
from pathlib import Path

_DEFAULT = r"G:\5、金融交易"


def _resolve_root() -> Path:
    p = Path(os.getenv("MARKET_DATA_DIR") or _DEFAULT)
    try:
        p.mkdir(parents=True, exist_ok=True)
        # 写测试，确认可用
        t = p / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return p
    except Exception:
        fb = Path(os.getenv("DATA_DIR", "./data"))
        fb.mkdir(parents=True, exist_ok=True)
        return fb


MARKET_ROOT  = _resolve_root()
KLINES_DIR   = MARKET_ROOT / "klines"
REGIME_DIR   = MARKET_ROOT / "regime_cache"
BACKTEST_DIR = MARKET_ROOT / "backtests"
for _d in (KLINES_DIR, REGIME_DIR, BACKTEST_DIR):
    _d.mkdir(parents=True, exist_ok=True)
