import json

import pytest

from backend.scripts.artifacts.create_model_release_manifest import (
    build_release_manifest,
    write_manifest_atomic,
)


def test_legacy_release_records_unknown_lineage_without_guessing(tmp_path):
    release = tmp_path / "release_20260709"
    release.mkdir()
    (release / "model.pkl").write_bytes(b"model")
    output = release / "release_manifest.json"

    payload = build_release_manifest(
        release,
        output=output,
        data_inventory_sha256="a" * 64,
    )

    assert payload["status"] == "legacy_incomplete"
    assert payload["lineage"]["complete"] is False
    assert payload["lineage"]["training_code_revision"] is None
    assert payload["artifact_count"] == 1
    assert payload["artifacts"][0]["name"] == "model.pkl"


def test_manifest_writes_once_and_refuses_to_replace_sealed_output(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "model.pkl").write_bytes(b"model")
    output = release / "release_manifest.json"
    payload = build_release_manifest(
        release,
        output=output,
        data_inventory_sha256="b" * 64,
        training_code_revision="abc123",
        training_data_snapshot="snapshot-1",
        cost_assumptions="taker=10bp;slippage=2bp",
    )
    write_manifest_atomic(output, payload)

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "documented"
    assert saved["artifact_count"] == 1
    assert not list(release.glob(".release_manifest.json.*.tmp"))

    with pytest.raises(FileExistsError, match="already sealed"):
        write_manifest_atomic(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == saved


def test_release_manifest_must_stay_inside_release(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "model.pkl").write_bytes(b"model")

    with pytest.raises(ValueError, match="inside the release"):
        build_release_manifest(
            release,
            output=tmp_path / "outside.json",
            data_inventory_sha256="c" * 64,
        )
