"""
交易日志：实盘/纸面下单逐条记录到 G 盘（每天一个 jsonl），用于事后对账与复盘。
"""
import json
from datetime import datetime

from ..datastore import MARKET_ROOT

JOURNAL_DIR = MARKET_ROOT / "journal"
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def append(entry: dict) -> None:
    try:
        entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **entry}
        fn = JOURNAL_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with open(fn, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_recent(n: int = 200) -> list:
    files = sorted(JOURNAL_DIR.glob("*.jsonl"), reverse=True)
    rows = []
    for fn in files:
        try:
            lines = fn.read_text(encoding="utf-8").splitlines()
            for ln in reversed(lines):
                if ln.strip():
                    rows.append(json.loads(ln))
                    if len(rows) >= n:
                        return rows
        except Exception:
            pass
    return rows
