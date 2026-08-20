from app.calculators import fees


def test_bolero_fee_known_value():
    assert round(fees.estimate_bolero_fee(1000), 3) == 7.625


def test_ing_fee_applies_minimum_for_small_trades():
    assert fees.estimate_ing_fee(100) == fees.ING_MIN_EUR  # 100*0.35% = 0.35 < €1 minimum


def test_ing_fee_percentage_for_larger_trades():
    assert round(fees.estimate_ing_fee(1000), 2) == 3.5


def test_ibkr_us_fee_applies_percentage_cap_for_small_trades():
    # 50 EUR trade: per-share fee and the $1 minimum both exceed 1% of trade value, so the
    # 1%-of-trade-value cap must be what actually binds.
    assert round(fees.estimate_ibkr_fee(50, is_us_listed=True), 3) == 0.5


def test_ibkr_us_fee_applies_minimum_for_midsize_trades():
    # 1000 EUR trade: per-share fee (~0.10) is below the $1 minimum, and the 1% cap (10)
    # doesn't bind -- the $1 minimum should win.
    assert round(fees.estimate_ibkr_fee(1000, is_us_listed=True), 2) == 1.0


def test_ibkr_eu_fee_applies_minimum():
    assert fees.estimate_ibkr_fee(1000, is_us_listed=False) == fees.IBKR_EU_MIN_EUR


def test_tob_equity_known_value():
    assert round(fees.estimate_tob(1000, "equity"), 2) == 3.5


def test_tob_accumulating_etf_higher_than_distributing():
    acc = fees.estimate_tob(1000, "etf_accumulating")
    dist = fees.estimate_tob(1000, "etf_distributing")
    assert acc > dist


def test_tob_caps_apply_on_large_trades():
    assert fees.estimate_tob(10_000_000, "etf_accumulating") == fees.TOB_RATES["etf_accumulating"][1]


def test_cheapest_broker_estimate_picks_lowest():
    result = fees.cheapest_broker_estimate(1000, is_us_listed=True)
    assert result["broker"] == "ibkr"  # $1 min beats Bolero's ~7.6 and ING's 3.5 at this size
    assert result["fee_eur"] == 1.0
