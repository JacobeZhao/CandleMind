import json
from pathlib import Path
from typing import List

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def _path(symbol: str, interval: str) -> Path:
    return DATA_DIR / f"{symbol}_{interval}.json"


def load_klines(symbol: str, interval: str) -> List[dict]:
    path = _path(symbol, interval)
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_klines(symbol: str, interval: str, klines: List[dict]):
    path = _path(symbol, interval)
    with path.open("w", encoding="utf-8") as f:
        json.dump(klines, f, ensure_ascii=False, default=str)
