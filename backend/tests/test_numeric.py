from app.numeric import finite


def test_finite_passes_through_real_numbers():
    assert finite(1.5) == 1.5
    assert finite(0.0) == 0.0
    assert finite(-42.0) == -42.0


def test_finite_converts_nan_to_none():
    assert finite(float("nan")) is None


def test_finite_converts_infinity_to_none():
    assert finite(float("inf")) is None
    assert finite(float("-inf")) is None


def test_finite_passes_through_none():
    assert finite(None) is None


def test_finite_handles_non_numeric_gracefully():
    # Defensive: some yfinance fields are occasionally the wrong type entirely --
    # must degrade to None, not raise and take the whole response down with it.
    assert finite("not a number") is None
