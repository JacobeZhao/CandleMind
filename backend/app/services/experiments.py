"""
实验登记表：每次回测/稳健性研究记一条，可检索、排行、对比。
独立 SQLite 库存 G 盘：MARKET_ROOT/experiments.db（与配置库 trader.db 分开）。
"""
import json
import sqlite3
import hashlib
import subprocess
from datetime import datetime

from ..datastore import MARKET_ROOT

DB_PATH = MARKET_ROOT / "experiments.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, kind TEXT, symbols TEXT, period TEXT,
            params_json TEXT, params_hash TEXT, metrics_json TEXT,
            net_pct REAL, oos_pct REAL, sharpe REAL, max_dd REAL, num_trades INTEGER,
            git_hash TEXT, data_hash TEXT, archive_path TEXT
        )""")
    return c


def _git_hash() -> str:
    from pathlib import Path
    proj = Path(__file__).resolve().parents[3]   # 项目根（若是 git 仓库）
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(proj),
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except Exception:
        return ""


def params_hash(params: dict) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]


def log_run(kind: str, symbols, period: str, params: dict, metrics: dict,
            archive_path: str = "", data_hash: str = "") -> int:
    syms = symbols if isinstance(symbols, str) else ",".join(symbols)
    c = _conn()
    cur = c.execute(
        """INSERT INTO runs (ts,kind,symbols,period,params_json,params_hash,metrics_json,
           net_pct,oos_pct,sharpe,max_dd,num_trades,git_hash,data_hash,archive_path)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, syms, period,
         json.dumps(params, ensure_ascii=False, default=str), params_hash(params),
         json.dumps(metrics, ensure_ascii=False, default=str),
         metrics.get("total_return_pct"), metrics.get("oos_pct"),
         metrics.get("sharpe"), metrics.get("max_drawdown"), metrics.get("num_trades"),
         _git_hash(), data_hash, archive_path))
    c.commit(); rid = cur.lastrowid; c.close()
    return rid


def _row(r) -> dict:
    cols = ["id", "ts", "kind", "symbols", "period", "params_json", "params_hash",
            "metrics_json", "net_pct", "oos_pct", "sharpe", "max_dd", "num_trades",
            "git_hash", "data_hash", "archive_path"]
    d = dict(zip(cols, r))
    for k in ("params_json", "metrics_json"):
        try: d[k] = json.loads(d[k]) if d[k] else None
        except Exception: pass
    return d


def list_runs(limit: int = 100, kind: str = None) -> list:
    c = _conn()
    q = "SELECT * FROM runs"
    args = []
    if kind:
        q += " WHERE kind=?"; args.append(kind)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall(); c.close()
    return [_row(r) for r in rows]


def get_run(rid: int) -> dict | None:
    c = _conn()
    r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone(); c.close()
    return _row(r) if r else None


def leaderboard(metric: str = "oos_pct", limit: int = 20) -> list:
    col = metric if metric in ("net_pct", "oos_pct", "sharpe", "max_dd", "num_trades") else "oos_pct"
    c = _conn()
    rows = c.execute(
        f"SELECT * FROM runs WHERE {col} IS NOT NULL ORDER BY {col} DESC LIMIT ?",
        (limit,)).fetchall(); c.close()
    return [_row(r) for r in rows]
