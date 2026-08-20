import time
from types import SimpleNamespace

from app.connectors import yfinance_client
from app.connectors.yfinance_client import _call_with_timeout, get_current_quote, get_fundamentals


def test_call_with_timeout_returns_result_when_fast():
    assert _call_with_timeout(lambda: 42, timeout=1) == 42


def test_call_with_timeout_returns_none_on_exception():
    def boom():
        raise ValueError("simulated failure")

    assert _call_with_timeout(boom, timeout=1) is None


def test_call_with_timeout_returns_none_when_call_hangs():
    # The actual bug this fixes: live-verified that under Yahoo Finance rate-limiting,
    # yfinance's own internal retry logic can block for minutes instead of failing fast.
    # A slow/hung call must degrade to None quickly, not block the caller indefinitely.
    def slow():
        time.sleep(5)
        return "too late"

    started = time.time()
    result = _call_with_timeout(slow, timeout=0.2)
    elapsed = time.time() - started

    assert result is None
    assert elapsed < 1  # bounded by the timeout, not by how long `slow` actually takes


def test_get_current_quote_returns_none_for_nan_price(monkeypatch):
    # NaN is truthy in Python -- Yahoo's own API can hand back a NaN price directly, and
    # without an explicit finite() check this would sail past `if not price` untouched.
    fake_ticker = SimpleNamespace(info={"regularMarketPrice": float("nan"), "currency": "USD"})
    monkeypatch.setattr(yfinance_client.yf, "Ticker", lambda ticker: fake_ticker)

    assert get_current_quote("AAPL") is None


def test_get_fundamentals_sanitizes_nan_fields(monkeypatch):
    fake_ticker = SimpleNamespace(
        info={
            "currentPrice": 100.0,
            "trailingPE": float("nan"),
            "forwardPE": 15.0,
            "shortName": "Test Co",
        }
    )
    monkeypatch.setattr(yfinance_client.yf, "Ticker", lambda ticker: fake_ticker)

    result = get_fundamentals("AAPL")

    assert result["price"] == 100.0
    assert result["trailing_pe"] is None  # sanitized, not a NaN that would crash the API later
    assert result["forward_pe"] == 15.0


def test_get_fundamentals_returns_none_for_nan_price(monkeypatch):
    fake_ticker = SimpleNamespace(info={"currentPrice": float("nan")})
    monkeypatch.setattr(yfinance_client.yf, "Ticker", lambda ticker: fake_ticker)

    assert get_fundamentals("AAPL") is None
