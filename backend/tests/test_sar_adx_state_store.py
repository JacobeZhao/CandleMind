from __future__ import annotations

import json

import pytest

from backend.app.services.sar_adx_state_store import SarAdxStateError, SarAdxStateStore


def _payload() -> dict:
    return {
        "config_version": "sar_adx_v3",
        "config_hash": "abc",
        "symbol": "SOLUSDT",
        "strategy": {},
        "broker": {},
    }


def test_state_store_atomically_round_trips(tmp_path) -> None:
    store = SarAdxStateStore(tmp_path)
    path = store.save("SOLUSDT", _payload())
    loaded = store.load("SOLUSDT", config_version="sar_adx_v3", config_hash="abc")
    assert loaded is not None and loaded["symbol"] == "SOLUSDT"
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_corrupted_or_incompatible_state_fails_closed(tmp_path) -> None:
    store = SarAdxStateStore(tmp_path)
    store.path_for("SOLUSDT").write_text("{broken", encoding="utf-8")
    with pytest.raises(SarAdxStateError, match="unreadable"):
        store.load("SOLUSDT", config_version="sar_adx_v3", config_hash="abc")
    store.save("SOLUSDT", _payload())
    with pytest.raises(SarAdxStateError, match="config_hash"):
        store.load("SOLUSDT", config_version="sar_adx_v3", config_hash="different")
