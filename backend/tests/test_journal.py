from types import SimpleNamespace

import pandas as pd

from app.journal import _last_price_at_or_before, calibration_summary


def _entry(confidence, return_14d=None, return_30d=None, return_90d=None):
    return SimpleNamespace(
        confidence_at_creation=confidence,
        return_14d=return_14d,
        return_30d=return_30d,
        return_90d=return_90d,
    )


def test_calibration_summary_buckets_by_confidence_decile():
    entries = [
        _entry(0.55, return_14d=0.02),  # hit, 50-60% bucket
        _entry(0.58, return_14d=-0.01),  # miss, 50-60% bucket
        _entry(0.72, return_14d=0.03),  # hit, 70-80% bucket
    ]
    result = calibration_summary(entries)

    assert result["total_entries"] == 3
    assert result["matured_entries"] == 3
    bucket_50 = result["calibration_buckets"]["50-60%"]
    assert bucket_50["num_entries"] == 2
    assert bucket_50["actual_hit_rate"] == 0.5

    bucket_70 = result["calibration_buckets"]["70-80%"]
    assert bucket_70["num_entries"] == 1
    assert bucket_70["actual_hit_rate"] == 1.0


def test_calibration_summary_ignores_unmatured_entries():
    entries = [_entry(0.6), _entry(0.6)]  # no horizons matured yet
    result = calibration_summary(entries)

    assert result["total_entries"] == 2
    assert result["matured_entries"] == 0
    assert result["calibration_buckets"] == {}


def test_calibration_summary_counts_one_entry_across_multiple_horizons():
    # a single entry with two matured horizons contributes to the bucket twice --
    # that's intentional, each horizon is its own evidence point
    entries = [_entry(0.65, return_14d=0.01, return_30d=-0.01)]
    result = calibration_summary(entries)

    assert result["matured_entries"] == 2
    assert result["calibration_buckets"]["60-70%"]["num_entries"] == 2


def test_last_price_at_or_before_picks_closest_prior_date():
    index = pd.to_datetime(["2026-01-01", "2026-01-05", "2026-01-10"], utc=True)
    hist = pd.DataFrame({"Close": [100.0, 105.0, 110.0]}, index=index)

    result = _last_price_at_or_before(hist, pd.Timestamp("2026-01-07", tz="UTC"))
    assert result == 105.0


def test_last_price_at_or_before_no_data_returns_none():
    index = pd.to_datetime(["2026-01-05"], utc=True)
    hist = pd.DataFrame({"Close": [100.0]}, index=index)

    result = _last_price_at_or_before(hist, pd.Timestamp("2026-01-01", tz="UTC"))
    assert result is None
