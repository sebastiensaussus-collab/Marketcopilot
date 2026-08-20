import math

import numpy as np
import pandas as pd

from app.calculators.risk import correlation, max_drawdown, sharpe_ratio


def test_max_drawdown_known_path():
    # 100 -> 120 (peak) -> 60 (trough) -> drawdown should be -0.5
    close = pd.Series([100.0, 110.0, 120.0, 90.0, 60.0, 80.0])
    result = max_drawdown(close)
    assert round(result, 6) == -0.5


def test_max_drawdown_monotonic_increase_is_zero():
    close = pd.Series([100.0, 105.0, 110.0, 120.0])
    assert max_drawdown(close) == 0.0


def test_sharpe_ratio_zero_variance_returns_none():
    # identical prices -> zero returns every day -> zero std -> guarded against div by zero
    close = pd.Series([100.0, 100.0, 100.0, 100.0])
    assert sharpe_ratio(close) is None


def test_sharpe_ratio_positive_trend():
    close = pd.Series([100, 102, 101, 105, 108, 107, 112, 115])
    result = sharpe_ratio(close)
    assert result is not None
    assert result > 0


def test_correlation_perfectly_correlated_series():
    a = pd.Series([100, 102, 104, 103, 106, 108])
    b = a * 2  # perfectly correlated, different scale
    result = correlation(a, b)
    assert round(result, 6) == 1.0


def test_correlation_insufficient_data_returns_none():
    a = pd.Series([100.0])
    b = pd.Series([50.0])
    assert correlation(a, b) is None


def test_max_drawdown_never_returns_nan_even_with_a_data_gap():
    # NaN isn't valid JSON -- live-verified real bug (a different NaN, in
    # technicals.py's pct_change) crashed an entire API response over exactly this class
    # of unguarded float. A data gap partway through a price series must not produce one.
    close = pd.Series([100.0, 110.0, np.nan, 90.0, 60.0, 80.0])
    result = max_drawdown(close)
    assert result is None or not math.isnan(result)


def test_sharpe_ratio_never_returns_nan_even_with_a_data_gap():
    close = pd.Series([100.0, 102.0, np.nan, 105.0, 108.0, 107.0, 112.0, 115.0])
    result = sharpe_ratio(close)
    assert result is None or not math.isnan(result)


def test_correlation_never_returns_nan():
    a = pd.Series([100.0, 100.0, 100.0, 100.0])  # zero variance -> corr is undefined (NaN internally)
    b = pd.Series([50.0, 52.0, 48.0, 51.0])
    result = correlation(a, b)
    assert result is None
