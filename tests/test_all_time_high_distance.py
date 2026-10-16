import pandas as pd

from src.analysis.indicators import compute_all_indicators


def test_all_time_high_distance_matches_reference_high_logic() -> None:
    df = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104, 105, 106, 105, 104],
            "high": [105, 106, 109, 110, 111, 112, 113, 110, 109],
            "low": [98, 99, 100, 101, 102, 103, 104, 103, 102],
            "close": [102, 103, 108, 109, 110, 111, 112, 107, 108],
            "volume": [1000] * 9,
        }
    )

    results = compute_all_indicators(df)

    assert results["all_time_high"] == 113.0
    assert abs(results["dist_from_all_time_high"] + 4.424778761061947) < 1e-6
