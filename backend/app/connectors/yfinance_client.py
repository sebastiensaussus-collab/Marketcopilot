"""Wraps yfinance for price history + fundamentals. Free, no API key.

Every call below runs through _call_with_timeout. Live-verified why that's necessary, not
just defensive: under sustained Yahoo Finance rate-limiting (HTTP 429 -- confirmed via a
direct curl during this session after heavy cumulative usage), yfinance's own internal
retry/backoff logic doesn't fail fast -- a single call can block for minutes rather than
raising quickly, and that blocks whatever pipeline called it (a scheduled report, the
dashboard) for just as long. A hard wall-clock timeout per call turns "hangs
indefinitely" into "degrades to None quickly," which every caller here already treats as
a normal, expected outcome (missing data for one symbol, not a crash).
"""

import threading
from datetime import datetime

import pandas as pd
import yfinance as yf

from app.numeric import finite

CALL_TIMEOUT_SECONDS = 10


def _call_with_timeout(fn, timeout: float = CALL_TIMEOUT_SECONDS):
    """Runs fn() on its own daemon thread and waits up to `timeout` seconds. Returns None
    on any exception or on timeout. A timed-out call's thread is abandoned, not killed
    (Python can't force-kill a thread) -- it keeps running harmlessly in the background as
    a daemon and its eventual result is simply discarded; nothing waits on it, so it can
    never block a later call the way a shared thread pool with a fixed worker count would.
    """
    result: dict = {}

    def runner():
        try:
            result["value"] = fn()
        except Exception:
            result["value"] = None

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return None
    return result.get("value")


def get_price_history(ticker: str, period: str = "1y") -> pd.DataFrame | None:
    hist = _call_with_timeout(lambda: yf.Ticker(ticker).history(period=period, auto_adjust=True))
    if hist is None or hist.empty:
        return None
    return hist


def get_price_history_range(ticker: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    """Like get_price_history but for an explicit past window -- yfinance's `period`
    param only accepts a fixed set of strings (1y, 6mo, ...), not arbitrary day counts,
    so reviewing a journal entry's specific holding window needs start/end instead."""
    hist = _call_with_timeout(lambda: yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True))
    if hist is None or hist.empty:
        return None
    return hist


def get_current_quote(ticker: str) -> dict | None:
    """Price + currency via yfinance's quote endpoint (`.info`), not `.history()`.
    Some listings (e.g. LSE-listed UCITS ETFs) return a live quote here but nothing at
    all from `.history()` via free yfinance -- this is the one that actually works for
    those, at the cost of not giving a historical series (see app/portfolio_risk.py).
    """
    info = _call_with_timeout(lambda: yf.Ticker(ticker).info)
    if not info:
        return None
    price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
    # finite() here, not just `if not price` -- NaN is truthy in Python, so a NaN price
    # (Yahoo's own API can hand one back) would otherwise sail straight past this check
    # and out into the rest of the app as a normal-looking float.
    price = finite(float(price)) if price else None
    if price is None:
        return None
    return {
        "ticker": ticker,
        "price": price,
        "currency": info.get("currency") or "USD",
        "name": info.get("longName") or info.get("shortName"),
    }


def get_fundamentals(ticker: str) -> dict | None:
    """Returns a normalized subset of yfinance's `.info` needed by the fundamentals calculator."""
    info = _call_with_timeout(lambda: yf.Ticker(ticker).info)
    if not info:
        return None

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    price = finite(float(price)) if price else None
    if price is None:
        return None

    return {
        "ticker": ticker,
        "name": info.get("shortName") or info.get("longName") or ticker,
        "price": price,
        "trailing_pe": finite(info.get("trailingPE")),
        "forward_pe": finite(info.get("forwardPE")),
        "price_to_book": finite(info.get("priceToBook")),
        "ev_to_ebitda": finite(info.get("enterpriseToEbitda")),
        "free_cashflow": finite(info.get("freeCashflow")),
        "market_cap": finite(info.get("marketCap")),
        "shares_outstanding": finite(info.get("sharesOutstanding")),
        "revenue_growth": finite(info.get("revenueGrowth")),
        "sector": info.get("sector"),
        "beta": finite(info.get("beta")),
        "fifty_two_week_high": finite(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": finite(info.get("fiftyTwoWeekLow")),
    }
