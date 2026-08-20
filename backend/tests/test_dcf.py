from app.calculators.dcf import fair_value_per_share, margin_of_safety


def test_fair_value_per_share_basic():
    result = fair_value_per_share(
        free_cashflow=1_000_000,
        shares_outstanding=100_000,
        growth_rate=0.05,
        discount_rate=0.10,
        terminal_growth_rate=0.02,
        projection_years=5,
    )
    assert result is not None
    assert result > 0


def test_fair_value_per_share_guards_invalid_discount_rate():
    # discount_rate <= terminal_growth_rate is not a valid DCF -> should not blow up
    result = fair_value_per_share(
        free_cashflow=1_000_000,
        shares_outstanding=100_000,
        growth_rate=0.05,
        discount_rate=0.02,
        terminal_growth_rate=0.02,
    )
    assert result is None


def test_fair_value_per_share_missing_inputs():
    assert fair_value_per_share(0, 100_000, 0.05, 0.10, 0.02) is None
    assert fair_value_per_share(1_000_000, 0, 0.05, 0.10, 0.02) is None


def test_margin_of_safety_undervalued():
    result = margin_of_safety(fair_value=150.0, current_price=100.0)
    assert round(result, 6) == 0.5


def test_margin_of_safety_overvalued():
    result = margin_of_safety(fair_value=80.0, current_price=100.0)
    assert round(result, 6) == -0.2


def test_margin_of_safety_missing_inputs():
    assert margin_of_safety(None, 100.0) is None
    assert margin_of_safety(150.0, None) is None


def test_fair_value_per_share_guards_discount_rate_of_negative_one():
    # (1 + discount_rate) ** year would be 0 -- plain Python division by zero raises
    # rather than silently returning inf/nan, but this must degrade gracefully like
    # every other invalid-input case here, not crash the caller.
    result = fair_value_per_share(
        free_cashflow=1_000_000,
        shares_outstanding=100_000,
        growth_rate=0.05,
        discount_rate=-1,
        terminal_growth_rate=0.02,
    )
    assert result is None
