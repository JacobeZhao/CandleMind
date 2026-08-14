"""Versioned, atomic persistence for SAR/ADX paper runtime state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from backend.app.runtime_paths import RUNTIME_DATA_DIR


SCHEMA_VERSION = 1
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")


class SarAdxStateError(RuntimeError):
    pass


class SarAdxStateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or RUNTIME_DATA_DIR / "strategies").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("invalid symbol")
        return self.root / f"sar_adx_paper_{symbol}.json"

    def load(self, symbol: str, *, config_version: str, config_hash: str) -> dict | None:
        path = self.path_for(symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SarAdxStateError(f"paper state is unreadable: {path}") from exc
        expected = {
            "schema_version": SCHEMA_VERSION,
            "config_version": config_version,
            "config_hash": config_hash,
            "symbol": symbol,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise SarAdxStateError(f"paper state {key} is incompatible")
        if not isinstance(payload.get("strategy"), dict) or not isinstance(payload.get("broker"), dict):
            raise SarAdxStateError("paper state payload is incomplete")
        return payload

    def save(self, symbol: str, payload: dict) -> Path:
        path = self.path_for(symbol)
        document = dict(payload)
        document["schema_version"] = SCHEMA_VERSION
        data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        temp_name: str | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.root)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            temp_name = None
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, TypeError, ValueError) as exc:
            raise SarAdxStateError(f"could not atomically save paper state: {path}") from exc
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
        return path
