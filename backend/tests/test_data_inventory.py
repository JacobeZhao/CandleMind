import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.scripts.artifacts.inventory_data_root import (
    REQUIRED_DIRECTORIES,
    build_inventory,
    validate_data_root,
    write_json_atomic,
)


def test_inventory_cli_is_runnable_from_arbitrary_working_directory(
    tmp_path: Path,
) -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "artifacts"
        / "inventory_data_root.py"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--root" in completed.stdout


def _data_root(tmp_path: Path) -> Path:
    for name in REQUIRED_DIRECTORIES:
        (tmp_path / name).mkdir()
    return tmp_path


def test_inventory_is_deterministic_and_reports_model_release(tmp_path):
    root = _data_root(tmp_path)
    release = root / "models" / "current" / "release_20260709"
    release.mkdir(parents=True)
    (release / "model.pkl").write_bytes(b"model")
    (root / "raw" / "bars.json").write_text("[]", encoding="utf-8")

    inventory = build_inventory(root, include_hashes=True)

    assert [entry["path"] for entry in inventory["files"]] == [
        "models/current/release_20260709/model.pkl",
        "raw/bars.json",
    ]
    assert inventory["file_count"] == 2
    assert inventory["model_releases"]["release_20260709"]["files"] == 1
    assert all(len(entry["sha256"]) == 64 for entry in inventory["files"])


def test_inventory_excludes_its_output_and_writes_atomically(tmp_path):
    root = _data_root(tmp_path)
    output = root / "manifests" / "inventory_current.json"
    output.write_text("stale", encoding="utf-8")

    inventory = build_inventory(root, excluded_paths=(output,))
    write_json_atomic(output, inventory)

    assert not any(entry["path"].endswith("inventory_current.json") for entry in inventory["files"])
    assert '"schema": "candlemind-data-inventory-v1"' in output.read_text(encoding="utf-8")
    assert not output.with_name(f".{output.name}.tmp").exists()


def test_validate_data_root_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing directories"):
        validate_data_root(tmp_path)


def test_validate_data_root_rejects_top_level_only_layout(tmp_path):
    for name in {Path(relative).parts[0] for relative in REQUIRED_DIRECTORIES}:
        (tmp_path / name).mkdir()

    with pytest.raises(ValueError, match="normalized/ohlcv_parquet"):
        validate_data_root(tmp_path)
