import numpy as np
import pandas as pd

from backend.app.services.indicators import REGISTRY, compute
from backend.app.strategies.sar_pyramid import parabolic_sar


def test_market_psar_matches_strategy_and_exposes_direction() -> None:
    close = np.array([100, 102, 105, 108, 104, 99, 95, 92, 97, 103, 108], dtype=float)
    bars = pd.DataFrame(
        {
            "high": close + 2,
            "low": close - 2,
            "close": close,
        }
    )

    market = compute(bars, "psar", {"step": 0.02, "max": 0.2})
    strategy = parabolic_sar(bars, step=0.02, maximum=0.2)

    pd.testing.assert_series_equal(market["psar"], strategy["psar"])
    pd.testing.assert_series_equal(
        market["psar_direction"],
        strategy["sar_direction"],
        check_names=False,
    )
    assert set(market["psar_direction"].iloc[1:]) == {-1, 1}
    assert REGISTRY["psar"]["outputs"] == ["psar", "psar_direction"]


def _trend_bars() -> pd.DataFrame:
    close = np.concatenate(
        [
            np.linspace(100, 145, 35),
            np.linspace(143, 78, 45),
            np.linspace(80, 132, 40),
        ]
    )
    spread = 1.5 + (np.arange(len(close)) % 4) * 0.2
    return pd.DataFrame(
        {
            "high": close + spread,
            "low": close - spread,
            "close": close,
        }
    )


def test_supertrend_registry_contract_and_default_parameters() -> None:
    meta = REGISTRY["supertrend"]

    assert meta["category"] == "trend"
    assert meta["panel"] == "main"
    assert meta["params"] == {"period": 10, "multiplier": 3.0}
    assert meta["outputs"] == ["supertrend", "supertrend_direction"]


def test_supertrend_tracks_trends_and_reversals_with_finite_values() -> None:
    bars = _trend_bars()
    result = compute(bars, "supertrend")
    line = result["supertrend"]
    direction = result["supertrend_direction"]

    assert np.isfinite(line.to_numpy()).all()
    assert set(direction) == {-1, 1}
    assert (direction.diff().fillna(0) != 0).sum() >= 2
    assert (line[direction == 1] <= bars.loc[direction == 1, "close"]).all()
    assert (line[direction == -1] >= bars.loc[direction == -1, "close"]).all()


def test_supertrend_is_prefix_invariant() -> None:
    bars = _trend_bars()
    complete = compute(bars, "supertrend")

    for prefix_length in (20, 50, 85, len(bars)):
        prefix = compute(bars.iloc[:prefix_length].copy(), "supertrend")
        pd.testing.assert_series_equal(
            prefix["supertrend"],
            complete["supertrend"].iloc[:prefix_length],
        )
        pd.testing.assert_series_equal(
            prefix["supertrend_direction"],
            complete["supertrend_direction"].iloc[:prefix_length],
        )
