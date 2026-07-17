import pytest

from backend.app.services.trend_predictor import (
    split_train_early_stop_calibration,
)
from backend.scripts.training.retrain_multi_horizon import split_retrain_windows


def _positions(window):
    return set(range(window.start, window.stop))


def _assert_disjoint_and_complete(split, n_samples):
    windows = [split.train, split.early_stop, split.calibration]
    if split.gate is not None:
        windows.append(split.gate)
    all_parts = windows + list(split.purged)

    occupied = set()
    for part in all_parts:
        positions = _positions(part)
        assert occupied.isdisjoint(positions)
        occupied.update(positions)
    assert occupied == set(range(n_samples))


def test_legacy_training_split_is_purged_ordered_and_mutually_exclusive():
    split = split_train_early_stop_calibration(
        10_000,
        50,
        min_train=100,
        min_early_stop=100,
        min_calibration=100,
    )

    assert split.train.stop == split.purged[0].start
    assert split.purged[0].size == 50
    assert split.purged[0].stop == split.early_stop.start
    assert split.early_stop.stop == split.purged[1].start
    assert split.purged[1].size == 50
    assert split.purged[1].stop == split.calibration.start
    assert split.calibration.stop == 10_000
    _assert_disjoint_and_complete(split, 10_000)


def test_legacy_training_split_fails_closed_when_samples_are_insufficient():
    with pytest.raises(ValueError, match="insufficient samples"):
        split_train_early_stop_calibration(
            1_000,
            50,
            min_train=500,
            min_early_stop=300,
            min_calibration=300,
        )


def test_multi_horizon_split_keeps_gate_isolated_from_all_model_use():
    split = split_retrain_windows(
        n_samples=9_000,
        holdout_start=6_000,
        purge_bars=12,
        min_train=500,
        min_window=200,
    )

    assert split.train.stop == 6_000 - 12
    assert split.purged[0].stop == split.early_stop.start == 6_000
    assert split.early_stop.stop == split.purged[1].start
    assert split.purged[1].stop == split.calibration.start
    assert split.calibration.stop == split.purged[2].start
    assert split.purged[2].stop == split.gate.start
    assert split.gate.stop == 9_000
    assert all(window.size == 12 for window in split.purged)
    model_use = (
        _positions(split.train)
        | _positions(split.early_stop)
        | _positions(split.calibration)
    )
    assert model_use.isdisjoint(_positions(split.gate))
    _assert_disjoint_and_complete(split, 9_000)


@pytest.mark.parametrize("holdout_start", [0, 8_500])
def test_multi_horizon_split_fails_closed_for_undersized_windows(holdout_start):
    with pytest.raises(ValueError, match="insufficient samples"):
        split_retrain_windows(
            n_samples=9_000,
            holdout_start=holdout_start,
            purge_bars=48,
            min_train=3_000,
            min_window=200,
        )
