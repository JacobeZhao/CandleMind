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
    assert "supertrend" not in REGISTRY
