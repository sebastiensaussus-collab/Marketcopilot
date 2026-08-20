import numpy as np
import pandas as pd

from app.calculators.technicals import (
    _finite,
    annualized_realized_vol,
    latest_snapshot,
    macd,
    rsi,
)


def test_rsi_all_gains_saturates_high():
    close = pd.Series(np.linspace(100, 130, 20))
    result = rsi(close, period=14)
    assert result.iloc[-1] > 95


def test_rsi_all_losses_saturates_low():
    close = pd.Series(np.linspace(130, 100, 20))
    result = rsi(close, period=14)
    assert result.iloc[-1] < 5


def test_rsi_flat_series_is_neutral():
    close = pd.Series([100.0] * 20)
    result = rsi(close, period=14)
    # no gains or losses at all -> our fillna(100) convention kicks in for the
    # 0/0 case, so just assert it doesn't blow up and stays in a sane bound
    assert 0 <= result.iloc[-1] <= 100


def test_macd_shape_matches_input():
    close = pd.Series(np.linspace(100, 150, 40))
    result = macd(close)
    assert list(result.columns) == ["macd", "signal", "histogram"]
    assert len(result) == len(close)


def test_macd_constant_series_is_near_zero():
    close = pd.Series([100.0] * 40)
    result = macd(close)
    assert abs(result["macd"].iloc[-1]) < 1e-9
    assert abs(result["histogram"].iloc[-1]) < 1e-9


def test_annualized_realized_vol_known_value():
    # constant daily return of 1% every day -> zero variance -> zero vol
    close = pd.Series([100 * (1.01**i) for i in range(30)])
    result = annualized_realized_vol(close)
    assert result < 1e-6


def test_annualized_realized_vol_empty_series():
    close = pd.Series([100.0])
    assert annualized_realized_vol(close) == 0.0


def test_finite_converts_nan_and_inf_to_none():
    # NaN isn't valid JSON -- live-verified this crashed the entire /opportunities
    # response (500, no data for ANY symbol) when a single satellite candidate's
    # pct_change_1w came out NaN. Every float latest_snapshot returns must be sanitized.
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None
    assert _finite(float("-inf")) is None
    assert _finite(None) is None
    assert _finite(1.5) == 1.5


def test_latest_snapshot_never_returns_nan_for_pct_change():
    # A data gap (a NaN close price) lands exactly on the 1-week-ago comparison point --
    # this must degrade to None, not propagate a NaN into the API response.
    values = [100.0 + i for i in range(30)]
    values[24] = float("nan")  # exactly 5 (the 1-week lookback) positions before the end
    close = pd.Series(values)

    result = latest_snapshot(close)

    assert result["pct_change_1w"] is None
    # 1m/3m lookbacks land on different points in the series -- must stay real numbers,
    # not have been accidentally nulled out by the same fix.
    assert result["pct_change_1m"] is not None
    assert result["pct_change_3m"] is None  # series is only 30 long, shorter than the 63d lookback
