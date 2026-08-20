from types import SimpleNamespace

from app.connectors import fx


def test_get_fx_rate_same_currency_no_network_call():
    assert fx.get_fx_rate("EUR", "EUR") == 1.0


def test_get_fx_rate_returns_none_for_nan_rate(monkeypatch):
    # NaN is truthy in Python -- without finite(), a NaN FX rate from yfinance would
    # sail through and poison every EUR-converted value it's multiplied into
    # (app/portfolio_risk.py's NAV, concentration weights, everything downstream).
    fake_ticker = SimpleNamespace(info={"regularMarketPrice": float("nan")})
    monkeypatch.setattr(fx.yf, "Ticker", lambda ticker: fake_ticker)

    assert fx.get_fx_rate("USD", "EUR") is None


def test_get_fx_rate_returns_real_rate(monkeypatch):
    fake_ticker = SimpleNamespace(info={"regularMarketPrice": 0.92})
    monkeypatch.setattr(fx.yf, "Ticker", lambda ticker: fake_ticker)

    assert fx.get_fx_rate("USD", "EUR") == 0.92


def test_get_fx_rate_none_when_lookup_fails(monkeypatch):
    monkeypatch.setattr(fx.yf, "Ticker", lambda ticker: SimpleNamespace(info={}))
    assert fx.get_fx_rate("USD", "EUR") is None
