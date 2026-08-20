from app.calculators.fundamentals import fcf_yield, summarize


def test_fcf_yield_basic():
    assert fcf_yield(10_000_000, 200_000_000) == 0.05


def test_fcf_yield_missing_inputs():
    assert fcf_yield(None, 200_000_000) is None
    assert fcf_yield(10_000_000, None) is None
    assert fcf_yield(10_000_000, 0) is None


def test_summarize_computes_fcf_yield_and_pct_off_high():
    fundamentals = {
        "ticker": "AAPL",
        "price": 180.0,
        "trailing_pe": 30.0,
        "forward_pe": 28.0,
        "price_to_book": 45.0,
        "ev_to_ebitda": 22.0,
        "free_cashflow": 90_000_000_000,
        "market_cap": 3_000_000_000_000,
        "beta": 1.2,
        "fifty_two_week_high": 200.0,
        "fifty_two_week_low": 150.0,
    }
    result = summarize(fundamentals)

    assert result["ticker"] == "AAPL"
    assert round(result["fcf_yield"], 4) == 0.03
    assert round(result["pct_off_52w_high"], 4) == round(180.0 / 200.0 - 1, 4)


def test_summarize_handles_missing_optional_fields():
    fundamentals = {"ticker": "XYZ", "price": None, "fifty_two_week_high": None}
    result = summarize(fundamentals)
    assert result["fcf_yield"] is None
    assert result["pct_off_52w_high"] is None


def test_fcf_yield_never_returns_nan():
    # market_cap of 0 already guarded (falsy) -- this covers the case where a genuinely
    # nonzero-but-degenerate input still produces NaN internally (e.g. inf / inf).
    assert fcf_yield(float("inf"), float("inf")) is None


def test_pct_off_high_via_summarize_never_returns_nan_or_inf():
    # A yfinance field that's itself NaN/inf -- possible directly from Yahoo's own API,
    # not just from something this codebase computed -- must not propagate through.
    fundamentals = {"ticker": "XYZ", "price": float("nan"), "fifty_two_week_high": 200.0}
    result = summarize(fundamentals)
    assert result["pct_off_52w_high"] is None
