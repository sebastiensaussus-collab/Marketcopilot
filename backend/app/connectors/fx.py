"""FX rates via yfinance's currency-pair tickers (e.g. EURUSD=X). Free, no key needed."""

import yfinance as yf

from app.connectors.yfinance_client import _call_with_timeout
from app.numeric import finite


def get_fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Units of to_ccy per 1 unit of from_ccy. Returns 1.0 for same-currency pairs
    without a network call."""
    if from_ccy == to_ccy:
        return 1.0
    info = _call_with_timeout(lambda: yf.Ticker(f"{from_ccy}{to_ccy}=X").info)
    if not info:
        return None
    rate = info.get("regularMarketPrice") or info.get("previousClose")
    # finite(), not just `if rate` -- NaN is truthy, so a NaN FX rate would otherwise
    # sail straight through and poison every EUR-converted value it multiplies into
    # (portfolio value, concentration weights, all downstream of app/portfolio_risk.py).
    return finite(float(rate)) if rate else None
