from app.sizing import (
    MAX_POSITION_PCT,
    kelly_fraction_even_money,
    suggested_size_satellite,
)


def test_kelly_fraction_even_money_known_value():
    # f* = 2p - 1
    assert round(kelly_fraction_even_money(0.74), 4) == 0.48


def test_kelly_fraction_even_money_coinflip_is_zero():
    assert kelly_fraction_even_money(0.5) == 0.0


def test_kelly_fraction_even_money_losing_edge_floors_at_zero():
    assert kelly_fraction_even_money(0.3) == 0.0


def test_suggested_size_satellite_realistic_confidence_is_not_zero():
    # Regression test: the first version compared confidence against an assumed 0.5
    # coin-flip baseline. Live testing showed Claude's actual satellite confidence
    # clusters at 0.40-0.50 (the prompt tells it to rarely exceed 0.75), so every real
    # thesis landed at or below 0.5 and every suggested size came back zero. This exact
    # confidence value (0.45, an AMD thesis from a live run) must be positive now.
    result = suggested_size_satellite(confidence=0.45)
    assert result["suggested_position_pct"] > 0.0


def test_suggested_size_satellite_floors_to_zero_below_min_confidence():
    result = suggested_size_satellite(confidence=0.25)
    assert result["suggested_position_pct"] == 0.0


def test_suggested_size_satellite_scales_with_confidence():
    low = suggested_size_satellite(confidence=0.40)
    high = suggested_size_satellite(confidence=0.70)
    assert high["suggested_position_pct"] > low["suggested_position_pct"]


def test_suggested_size_satellite_respects_hard_cap():
    result = suggested_size_satellite(confidence=1.0)
    assert result["suggested_position_pct"] <= MAX_POSITION_PCT


def test_suggested_size_satellite_never_negative():
    result = suggested_size_satellite(confidence=0.0)
    assert result["suggested_position_pct"] == 0.0
