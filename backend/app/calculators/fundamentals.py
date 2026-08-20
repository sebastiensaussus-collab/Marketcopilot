"""Fundamental ratio calculations from a yfinance-derived fundamentals dict."""

from app.numeric import finite


def fcf_yield(free_cashflow: float | None, market_cap: float | None) -> float | None:
    if not free_cashflow or not market_cap:
        return None
    return finite(free_cashflow / market_cap)


def summarize(fundamentals: dict) -> dict:
    """fundamentals: output of connectors.yfinance_client.get_fundamentals()."""
    return {
        "ticker": fundamentals["ticker"],
        "trailing_pe": fundamentals.get("trailing_pe"),
        "forward_pe": fundamentals.get("forward_pe"),
        "price_to_book": fundamentals.get("price_to_book"),
        "ev_to_ebitda": fundamentals.get("ev_to_ebitda"),
        "fcf_yield": fcf_yield(fundamentals.get("free_cashflow"), fundamentals.get("market_cap")),
        "beta": fundamentals.get("beta"),
        "pct_off_52w_high": _pct_off_high(
            fundamentals.get("price"), fundamentals.get("fifty_two_week_high")
        ),
    }


def _pct_off_high(price: float | None, high: float | None) -> float | None:
    if not price or not high:
        return None
    return finite(price / high - 1)
