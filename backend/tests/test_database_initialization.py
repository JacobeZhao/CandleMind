import json
import os
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run_database_contract(tmp_path: Path, script: str) -> dict:
    environment = os.environ.copy()
    environment["DATA_DIR"] = str(tmp_path.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_fresh_database_contains_only_retained_application_tables(tmp_path):
    result = _run_database_contract(
        tmp_path,
        """
import json
from sqlalchemy import inspect
from backend.app import database

database.init_db()
session = database.SessionLocal()
try:
    result = {
        "tables": sorted(inspect(database.engine).get_table_names()),
        "settings": session.query(database.Settings).count(),
        "ai_configs": session.query(database.AIConfig).count(),
        "strategy_configuration": session.query(database.StrategyConfiguration).count(),
    }
finally:
    session.close()
    database.engine.dispose()
print(json.dumps(result))
""",
    )

    assert result == {
        "tables": ["ai_configs", "settings", "strategy_configuration"],
        "settings": 1,
        "ai_configs": 0,
        "strategy_configuration": 1,
    }


def test_initialization_preserves_unmapped_legacy_strategy_table(tmp_path):
    result = _run_database_contract(
        tmp_path,
        """
import json
import os
import sqlite3
from pathlib import Path

database_path = Path(os.environ["DATA_DIR"]) / "trader.db"
connection = sqlite3.connect(database_path)
connection.execute(
    "CREATE TABLE strategies (legacy_id TEXT PRIMARY KEY, payload BLOB NOT NULL)"
)
connection.execute(
    "INSERT INTO strategies (legacy_id, payload) VALUES (?, ?)",
    ("sentinel", sqlite3.Binary(b"\\x00legacy\\xff")),
)
connection.commit()

def snapshot():
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategies'"
    ).fetchone()[0]
    columns = connection.execute("PRAGMA table_info(strategies)").fetchall()
    rows = [
        [legacy_id, payload.hex()]
        for legacy_id, payload in connection.execute(
            "SELECT legacy_id, payload FROM strategies ORDER BY legacy_id"
        ).fetchall()
    ]
    return {"sql": table_sql, "columns": columns, "rows": rows}

before = snapshot()
connection.close()

from backend.app import database
database.init_db()
database.engine.dispose()

connection = sqlite3.connect(database_path)
after = snapshot()
tables = sorted(
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
)
connection.close()
print(json.dumps({"before": before, "after": after, "tables": tables}))
""",
    )

    assert result["after"] == result["before"]
    assert result["tables"] == [
        "ai_configs",
        "settings",
        "strategies",
        "strategy_configuration",
    ]
