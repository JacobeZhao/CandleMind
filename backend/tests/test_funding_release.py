from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from backend.app.services.funding_contract import (
    FUNDING_GAP_TOLERANCE_MS,
    FUNDING_INTERVAL_MS,
    FUNDING_MAX_GAP_MS,
)
from backend.app.services.funding_release import (
    FundingReleaseError,
    canonical_derivatives_manifest_sha256,
    load_observed_funding_symbol,
    verify_observed_funding_release,
)
from backend.app.services import market_release
from backend.app.services.derivatives_data_sync import canonical_manifest_sha256
from backend.app.data_layout import REQUIRED_DIRECTORIES


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release(
    root: Path,
    *,
    symbol: str = "SOLUSDT",
    rates: tuple[float, ...] = (0.0001, -0.0002),
    event_times: tuple[int, ...] = (1_735_689_600_000, 1_735_718_400_000),
    available_times: tuple[int, ...] | None = None,
) -> dict:
    available_times = available_times or event_times
    path = root / "funding" / f"{symbol}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "symbol": [symbol] * len(rates),
            "event_time": event_times,
            "available_at": available_times,
            "funding_rate": rates,
        }
    )
    frame.to_parquet(path, index=False)
    manifest = {
        "schema": "candlemind-derivatives-release-v1",
        "release_id": "funding-test-v1",
        "status": "completed",
        "complete_universe": True,
        "datasets": ["funding"],
        "errors": [],
        "symbols": [symbol],
        "output_count": 1,
        "outputs": [
            {
                "dataset": "funding",
                "symbol": symbol,
                "path": f"funding/{symbol}.parquet",
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "rows": len(frame),
                "start_utc": pd.Timestamp(event_times[0], unit="ms", tz="UTC").isoformat(),
                "end_utc": pd.Timestamp(event_times[-1], unit="ms", tz="UTC").isoformat(),
            }
        ],
    }
    manifest["manifest_sha256"] = canonical_derivatives_manifest_sha256(manifest)
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _rewrite_manifest(root: Path, manifest: dict) -> None:
    manifest["manifest_sha256"] = canonical_derivatives_manifest_sha256(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_valid_funding_release_verifies_and_loads(tmp_path: Path) -> None:
    manifest = _write_release(tmp_path)

    assert verify_observed_funding_release(tmp_path) == manifest
    frame, record = load_observed_funding_symbol(
        tmp_path, "SOLUSDT", manifest=manifest
    )

    assert frame["funding_rate"].tolist() == pytest.approx([0.0001, -0.0002])
    assert record == manifest["outputs"][0]


def test_funding_gap_contract_uses_exact_millisecond_values() -> None:
    assert FUNDING_INTERVAL_MS == 28_800_000
    assert FUNDING_GAP_TOLERANCE_MS == 60_000
    assert FUNDING_MAX_GAP_MS == 28_860_000


@pytest.mark.parametrize(
    ("gap_ms", "accepted"),
    [
        (28_860_000, True),
        (28_860_001, False),
    ],
)
def test_funding_release_enforces_exact_internal_gap_boundary(
    tmp_path: Path, gap_ms: int, accepted: bool
) -> None:
    first_event = 1_735_689_600_000
    _write_release(
        tmp_path,
        event_times=(first_event, first_event + gap_ms),
    )

    if accepted:
        verify_observed_funding_release(tmp_path)
        return

    with pytest.raises(FundingReleaseError, match="coverage gap exceeds contract"):
        verify_observed_funding_release(tmp_path)


def test_canonical_hash_matches_derivatives_release_contract() -> None:
    payload = {
        "release_id": "funding-test-v1",
        "symbols": ["BTCUSDT", "SOLUSDT"],
        "metadata": {"unicode": "\u8d44\u91d1", "enabled": True},
        "manifest_sha256": "ignored",
    }

    assert canonical_derivatives_manifest_sha256(payload) == canonical_manifest_sha256(
        payload
    )


def test_funding_release_rejects_file_and_manifest_tampering(tmp_path: Path) -> None:
    manifest = _write_release(tmp_path)
    path = tmp_path / manifest["outputs"][0]["path"]
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(FundingReleaseError, match="byte evidence"):
        verify_observed_funding_release(tmp_path)


def test_funding_release_rejects_wrong_schema(tmp_path: Path) -> None:
    manifest = _write_release(tmp_path)
    manifest["schema"] = "unrelated-release-v1"
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(FundingReleaseError, match="incomplete"):
        verify_observed_funding_release(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.pop("start_utc"),
        lambda record: record.update(start_utc="not-a-time"),
        lambda record: record.update(start_utc="2025-01-01T00:00:00"),
        lambda record: record.update(start_utc="2024-01-01T00:00:00+00:00"),
        lambda record: record.pop("end_utc"),
        lambda record: record.update(end_utc="not-a-time"),
        lambda record: record.update(end_utc="2025-01-01T08:00:00"),
        lambda record: record.update(end_utc="2026-01-01T00:00:00+00:00"),
    ],
)
def test_funding_release_binds_manifest_coverage_to_actual_events(
    tmp_path: Path, mutation
) -> None:
    manifest = _write_release(tmp_path)
    mutation(manifest["outputs"][0])
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(FundingReleaseError, match="malformed|coverage evidence"):
        verify_observed_funding_release(tmp_path)

    _write_release(tmp_path)
    persisted = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    persisted["release_id"] = "changed"
    (tmp_path / "manifest.json").write_text(json.dumps(persisted), encoding="utf-8")
    with pytest.raises(FundingReleaseError, match="self-hash"):
        verify_observed_funding_release(tmp_path)


@pytest.mark.parametrize(
    ("rates", "events", "available", "message"),
    [
        ((float("nan"),), (1_735_689_600_000,), None, "values"),
        ((float("inf"),), (1_735_689_600_000,), None, "values"),
        ((float("-inf"),), (1_735_689_600_000,), None, "values"),
        ((1.0,), (1_735_689_600_000,), None, "values"),
        ((-1.0,), (1_735_689_600_000,), None, "values"),
        ((0.1,), (float("nan"),), None, "values"),
        (
            (0.1,),
            (1_735_689_600_000,),
            (float("nan"),),
            "values",
        ),
        ((0.1, 0.2), (1_735_689_600_000, 1_735_689_600_000), None, "values"),
        ((0.1, 0.2), (1_735_718_400_000, 1_735_689_600_000), None, "values"),
        (
            (0.1,),
            (1_735_689_600_000,),
            (1_735_689_599_999,),
            "values",
        ),
        (
            (0.1, 0.2),
            (1_735_689_600_000, 1_735_718_461_000),
            None,
            "coverage gap",
        ),
    ],
)
def test_funding_release_rejects_malformed_values(
    tmp_path: Path,
    rates: tuple[float, ...],
    events: tuple[int, ...],
    available: tuple[int, ...] | None,
    message: str,
) -> None:
    _write_release(
        tmp_path, rates=rates, event_times=events, available_times=available
    )

    with pytest.raises(FundingReleaseError, match=message):
        verify_observed_funding_release(tmp_path)


def test_funding_release_rejects_bad_symbol_path_and_unregistered_file(
    tmp_path: Path,
) -> None:
    manifest = _write_release(tmp_path)
    manifest["symbols"] = ["../SOLUSDT"]
    manifest["outputs"][0]["symbol"] = "../SOLUSDT"
    manifest["outputs"][0]["path"] = "funding/../SOLUSDT.parquet"
    _rewrite_manifest(tmp_path, manifest)
    with pytest.raises(FundingReleaseError, match="universe is malformed"):
        verify_observed_funding_release(tmp_path)

    manifest = _write_release(tmp_path)
    manifest["outputs"][0]["path"] = "../SOLUSDT.parquet"
    _rewrite_manifest(tmp_path, manifest)
    with pytest.raises(FundingReleaseError, match="malformed"):
        verify_observed_funding_release(tmp_path)

    _write_release(tmp_path)
    (tmp_path / "unexpected.txt").write_text("not registered", encoding="ascii")
    with pytest.raises(FundingReleaseError, match="unregistered files"):
        verify_observed_funding_release(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.update(outputs="invalid"),
        lambda manifest: manifest.update(outputs=[None]),
        lambda manifest: manifest["outputs"].append(dict(manifest["outputs"][0])),
        lambda manifest: manifest["outputs"][0].update(symbol=123),
        lambda manifest: manifest["outputs"][0].update(path=r"funding\\SOLUSDT.parquet"),
        lambda manifest: manifest["outputs"][0].update(path="/funding/SOLUSDT.parquet"),
        lambda manifest: manifest["outputs"][0].update(path="funding//SOLUSDT.parquet"),
        lambda manifest: manifest["outputs"][0].update(path="funding/../SOLUSDT.parquet"),
        lambda manifest: manifest["outputs"][0].update(rows="2"),
    ],
)
def test_funding_release_rejects_malformed_or_duplicate_outputs(
    tmp_path: Path, mutation
) -> None:
    manifest = _write_release(tmp_path)
    mutation(manifest)
    _rewrite_manifest(tmp_path, manifest)

    with pytest.raises(FundingReleaseError, match="malformed"):
        verify_observed_funding_release(tmp_path)


def test_funding_release_rejects_symlinked_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_release(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.name == "SOLUSDT.parquet" or original(path),
    )

    with pytest.raises(FundingReleaseError, match="symbolic link"):
        verify_observed_funding_release(tmp_path)


def test_funding_release_rejects_symlinked_release_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    _write_release(real_root)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(FundingReleaseError, match="symbolic link"):
        verify_observed_funding_release(alias)


def test_market_release_facade_preserves_historical_verifier_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {"schema": "candlemind-ema-data-release-v2", "release_id": "v2"}
    captured = []
    monkeypatch.setattr(
        market_release,
        "verify_ema_data_release",
        lambda path: captured.append(path) or expected,
    )

    assert market_release.verify_market_release(tmp_path) is expected
    assert captured == [tmp_path]


def test_funding_import_loads_no_ema_or_rl_modules(tmp_path: Path) -> None:
    market_root = tmp_path / "market"
    runtime_root = tmp_path / "runtime"
    market_root.mkdir()
    runtime_root.mkdir()
    environment = os.environ.copy()
    environment["MARKET_DATA_DIR"] = str(market_root.resolve())
    environment["DATA_DIR"] = str(runtime_root.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = """
import json
import sys
import backend.app.services.funding_release
print(json.dumps(sorted(
    name for name in sys.modules
    if "ema_" in name or name == "backend.app.rl" or name.startswith("backend.app.rl.")
)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_market_release_import_loads_no_rl_modules(tmp_path: Path) -> None:
    market_root = tmp_path / "market"
    runtime_root = tmp_path / "runtime"
    market_root.mkdir()
    runtime_root.mkdir()
    environment = os.environ.copy()
    environment["MARKET_DATA_DIR"] = str(market_root.resolve())
    environment["DATA_DIR"] = str(runtime_root.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = """
import json
import sys
import backend.app.services.market_release
print(json.dumps(sorted(
    name for name in sys.modules
    if name == "backend.app.rl" or name.startswith("backend.app.rl.")
)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_sar_import_does_not_load_ema_setup_input_release(tmp_path: Path) -> None:
    market_root = tmp_path / "market"
    runtime_root = tmp_path / "runtime"
    market_root.mkdir()
    runtime_root.mkdir()
    for relative in REQUIRED_DIRECTORIES:
        (market_root / relative).mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["MARKET_DATA_DIR"] = str(market_root.resolve())
    environment["DATA_DIR"] = str(runtime_root.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = """
import json
import sys
import backend.app.services.sar_adx_backtest
print(json.dumps({
    "setup_loaded": "backend.app.services.ema_setup_input_release" in sys.modules,
    "data_loaded": "backend.app.services.ema_data_release" in sys.modules,
    "feature_loaded": "backend.app.services.ema_feature_release" in sys.modules,
    "forbidden_ema_rl": sorted(
        name for name in sys.modules
        if name.startswith("backend.app.rl.ema_")
        and name != "backend.app.rl.ema_universe"
    ),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "setup_loaded": False,
        "data_loaded": True,
        "feature_loaded": False,
        "forbidden_ema_rl": [],
    }
