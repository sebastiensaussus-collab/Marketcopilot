"""Estimated trading costs, used only to flag when a suggested trade is fee-inefficient
at its size -- NOT exact costs. Sebastien's real fee schedule depends on his account
tier/agreements with each broker, which he opted not to supply (chose "public defaults,
flagged as estimates" when asked). Every constant below is sourced from each broker's
published standard tariff and must stay visibly labeled as an estimate wherever it
surfaces in output. If real numbers ever differ, correct the constants directly.
"""

# Bolero (KBC), standard published tariff, sourced May 2026: EUR7.50 + 0.0125% per order,
# applies the same on Belgian/European and US venues per the public tariff sheet.
BOLERO_FLAT_EUR = 7.50
BOLERO_PCT = 0.000125

# ING Belgium Self Invest, reduced Euronext BE/FR/NL tariff, sourced May 2026: 0.35% per
# order, EUR1 minimum.
ING_PCT = 0.0035
ING_MIN_EUR = 1.0

# IBKR Fixed pricing -- long-published, stable structure. US stocks: $0.005/share, $1
# min, capped at 1% of trade value (share count estimated from trade value / $50 assumed
# share price when not known -- see estimate_ibkr_fee). EU stocks: ~0.05% of trade value,
# EUR4 minimum.
IBKR_US_PER_SHARE_USD = 0.005
IBKR_US_MIN_USD = 1.0
IBKR_US_MAX_PCT = 0.01
IBKR_EU_PCT = 0.0005
IBKR_EU_MIN_EUR = 4.0
IBKR_ASSUMED_SHARE_PRICE_USD = 50.0  # only used to back out an approximate share count

# Belgian Tax on Stock Exchange Transactions (TOB / beurstaks) -- applies to a Belgian
# resident's trade regardless of broker, on top of commission. Rate depends on
# instrument type/domicile, not broker.
TOB_RATES = {
    "equity": (0.0035, 1600),
    "etf_distributing": (0.0012, 1600),
    "etf_accumulating": (0.0132, 4000),
}


def estimate_bolero_fee(trade_value_eur: float) -> float:
    return BOLERO_FLAT_EUR + trade_value_eur * BOLERO_PCT


def estimate_ing_fee(trade_value_eur: float) -> float:
    return max(trade_value_eur * ING_PCT, ING_MIN_EUR)


def estimate_ibkr_fee(trade_value_eur: float, is_us_listed: bool) -> float:
    if is_us_listed:
        shares = trade_value_eur / IBKR_ASSUMED_SHARE_PRICE_USD
        fee = shares * IBKR_US_PER_SHARE_USD
        fee = max(fee, IBKR_US_MIN_USD)
        return min(fee, trade_value_eur * IBKR_US_MAX_PCT)
    return max(trade_value_eur * IBKR_EU_PCT, IBKR_EU_MIN_EUR)


def estimate_tob(trade_value_eur: float, instrument_type: str) -> float:
    """instrument_type: 'equity' | 'etf_distributing' | 'etf_accumulating'."""
    rate, cap = TOB_RATES[instrument_type]
    return min(trade_value_eur * rate, cap)


def cheapest_broker_estimate(trade_value_eur: float, is_us_listed: bool) -> dict:
    """Picks the lowest-commission broker among Bolero/ING/IBKR for a given trade size.
    Estimate only -- see module docstring."""
    candidates = {
        "bolero": estimate_bolero_fee(trade_value_eur),
        "ing": estimate_ing_fee(trade_value_eur),
        "ibkr": estimate_ibkr_fee(trade_value_eur, is_us_listed),
    }
    broker = min(candidates, key=candidates.get)
    fee_eur = candidates[broker]
    fee_pct = fee_eur / trade_value_eur if trade_value_eur > 0 else None
    return {"broker": broker, "fee_eur": round(fee_eur, 2), "fee_pct": fee_pct}
