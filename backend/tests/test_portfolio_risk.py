from types import SimpleNamespace

from app import portfolio_risk
from app.portfolio_risk import (
    CONCENTRATION_THRESHOLD,
    HIGH_CORRELATION_THRESHOLD,
    _cached_resolve,
    _concentration,
    _names_by_symbol,
    _resolve_quote,
    _summarize_correlation_pairs,
    _value_in_account_ccy,
)


def test_concentration_computes_weights_and_flags_above_threshold():
    priced = [
        {"symbol": "AAA", "quantity": 1, "value_eur": 750.0, "source": "ibkr"},
        {"symbol": "BBB", "quantity": 1, "value_eur": 250.0, "source": "ibkr"},
    ]
    result = _concentration(priced)

    assert result["total_value_eur"] == 1000.0
    assert result["weights"] == {"AAA": 0.75, "BBB": 0.25}
    assert result["concentration_flags"] == [{"symbol": "AAA", "weight": 0.75}]


def test_concentration_no_flags_when_well_diversified():
    # 5-way even split -> 20% each, below the 25% threshold
    priced = [{"symbol": s, "quantity": 1, "value_eur": 200.0, "source": "ibkr"} for s in "ABCDE"]
    result = _concentration(priced)
    assert result["concentration_flags"] == []


def test_concentration_combines_same_symbol_across_sources():
    priced = [
        {"symbol": "NVDA", "quantity": 1, "value_eur": 200.0, "source": "ibkr"},
        {"symbol": "NVDA", "quantity": 6, "value_eur": 800.0, "source": "ing"},
    ]
    result = _concentration(priced)
    assert result["total_value_eur"] == 1000.0
    assert result["weights"] == {"NVDA": 1.0}


def test_concentration_empty_portfolio():
    result = _concentration([])
    assert result == {"total_value_eur": 0.0, "weights": {}, "concentration_flags": []}


def test_concentration_threshold_boundary_is_exclusive():
    # AAA sits exactly at the threshold -- should not be flagged, only strictly above it.
    # Spread the remainder across enough symbols that none of them trips the flag either.
    remaining_each = (1 - CONCENTRATION_THRESHOLD) / 4
    priced = [{"symbol": "AAA", "quantity": 1, "value_eur": CONCENTRATION_THRESHOLD * 100, "source": "ibkr"}] + [
        {"symbol": s, "quantity": 1, "value_eur": remaining_each * 100, "source": "ibkr"} for s in "BCDE"
    ]
    result = _concentration(priced)
    assert result["concentration_flags"] == []


def test_summarize_correlation_pairs_average():
    pairs = [
        {"symbol_a": "A", "symbol_b": "B", "correlation": 0.5},
        {"symbol_a": "A", "symbol_b": "C", "correlation": 0.9},
    ]
    result = _summarize_correlation_pairs(pairs)
    assert result["avg_pairwise_correlation"] == 0.7


def test_summarize_correlation_pairs_flags_high_correlation():
    pairs = [
        {"symbol_a": "A", "symbol_b": "B", "correlation": HIGH_CORRELATION_THRESHOLD + 0.05},
        {"symbol_a": "A", "symbol_b": "C", "correlation": 0.2},
    ]
    result = _summarize_correlation_pairs(pairs)
    assert len(result["highly_correlated_pairs"]) == 1
    assert result["highly_correlated_pairs"][0]["symbol_b"] == "B"


def test_summarize_correlation_pairs_empty():
    result = _summarize_correlation_pairs([])
    assert result == {"avg_pairwise_correlation": None, "highly_correlated_pairs": []}


def test_value_in_account_ccy_same_currency_no_network():
    # EUR->EUR short-circuits in fx.get_fx_rate without a network call
    assert _value_in_account_ccy(price=10.0, currency="EUR", quantity=5) == 50.0


def test_names_by_symbol_prefers_resolved_name_over_bare_symbol():
    priced = [{"symbol": "EUNA", "name": "ISHARES GLB AGG EUR-H ACC", "quantity": 1, "value_eur": 1.0, "source": "ibkr"}]
    assert _names_by_symbol(priced) == {"EUNA": "ISHARES GLB AGG EUR-H ACC"}


def test_names_by_symbol_falls_back_to_symbol_when_name_missing():
    priced = [{"symbol": "AGGH", "name": None, "quantity": 1, "value_eur": 1.0, "source": "bolero"}]
    assert _names_by_symbol(priced) == {"AGGH": "AGGH"}


def _fx_identity(monkeypatch):
    # EUR->EUR is 1.0 without a network call already in app/connectors/fx.py; USD->EUR
    # mocked to a fixed rate here so these tests don't depend on live FX data.
    monkeypatch.setattr(
        portfolio_risk.fx, "get_fx_rate", lambda frm, to: 1.0 if frm == to else (0.88 if frm == "USD" else None)
    )


def test_resolve_quote_disambiguates_ticker_collision_using_cost_basis(monkeypatch):
    # Live-verified real bug: bare "AGGH" resolves to an unrelated US-listed "Simplify
    # Aggregate Bond ETF" ($19.86) while the actually-held iShares UCITS fund only shows
    # up under ".L" (~EUR5.21). His real Bolero cost basis (4.95) must pick the .L one,
    # not whichever suffix happens to resolve first.
    def fake_quote(ticker):
        if ticker == "AGGH":
            return {"ticker": "AGGH", "price": 19.86, "currency": "USD", "name": "Simplify Aggregate Bond ETF"}
        if ticker == "AGGH.L":
            return {"ticker": "AGGH.L", "price": 5.21, "currency": "EUR", "name": "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)"}
        return None

    _fx_identity(monkeypatch)
    monkeypatch.setattr(portfolio_risk.yfinance_client, "get_current_quote", fake_quote)

    quote = _resolve_quote("AGGH", average_cost=4.95)

    assert quote["name"] == "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)"
    assert quote["price"] == 5.21


def test_resolve_quote_stops_early_when_first_match_is_close_enough(monkeypatch):
    # Performance: a real position's price vs. its cost basis drifts over time without
    # being a different security, so the common case (correct ticker, no collision) must
    # accept the first match and stop -- not pay for every remaining suffix lookup on
    # every single holding, every single request.
    calls = []

    def fake_quote(ticker):
        calls.append(ticker)
        if ticker == "NVDA":
            return {"ticker": "NVDA", "price": 224.09, "currency": "USD", "name": "NVIDIA Corporation"}
        return {"ticker": ticker, "price": 999.0, "currency": "EUR", "name": "should never be reached"}

    _fx_identity(monkeypatch)
    monkeypatch.setattr(portfolio_risk.yfinance_client, "get_current_quote", fake_quote)

    quote = _resolve_quote("NVDA", average_cost=174.22)

    assert quote["name"] == "NVIDIA Corporation"
    assert calls == ["NVDA"]  # stopped after the first (and only) lookup


def test_resolve_quote_disambiguation_is_fx_aware_not_raw_price(monkeypatch):
    # Live-verified regression in an earlier version of this fix: comparing raw,
    # unconverted prices picked NVDA's unrelated EUR130 Xetra listing ("61") over the
    # correct $224 US listing, purely because 130 sat numerically closer to a EUR cost
    # basis than unconverted 224 did. Once converted to EUR (~197), the real listing is
    # actually closer and must win.
    def fake_quote(ticker):
        if ticker == "NVDA":
            return {"ticker": "NVDA", "price": 224.09, "currency": "USD", "name": "NVIDIA Corporation"}
        if ticker == "NVDA.DE":
            return {"ticker": "NVDA.DE", "price": 130.0, "currency": "EUR", "name": "61"}
        return None

    _fx_identity(monkeypatch)
    monkeypatch.setattr(portfolio_risk.yfinance_client, "get_current_quote", fake_quote)

    quote = _resolve_quote("NVDA", average_cost=174.22)

    assert quote["name"] == "NVIDIA Corporation"


def test_resolve_quote_falls_back_to_first_match_without_cost_basis(monkeypatch):
    # No cost basis on file (e.g. average_cost wasn't in the CSV) -- can't disambiguate,
    # so this must not crash, just keep the old first-match behavior.
    monkeypatch.setattr(
        portfolio_risk.yfinance_client,
        "get_current_quote",
        lambda ticker: {"ticker": ticker, "price": 100.0, "currency": "USD", "name": None} if ticker == "XYZ" else None,
    )

    quote = _resolve_quote("XYZ", average_cost=None)

    assert quote["price"] == 100.0


def test_resolve_quote_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(portfolio_risk.yfinance_client, "get_current_quote", lambda ticker: None)
    assert _resolve_quote("NOPE", average_cost=10.0) is None


def test_names_by_symbol_first_seen_wins_across_sources():
    priced = [
        {"symbol": "AAPL", "name": "Apple Inc.", "quantity": 1, "value_eur": 100.0, "source": "ibkr"},
        {"symbol": "AAPL", "name": None, "quantity": 1, "value_eur": 50.0, "source": "ing"},
    ]
    assert _names_by_symbol(priced) == {"AAPL": "Apple Inc."}


def test_cached_resolve_returns_cached_quote_without_calling_resolver(monkeypatch):
    monkeypatch.setattr(
        portfolio_risk,
        "get_fresh_quote_cache",
        lambda symbol, max_age_seconds: SimpleNamespace(found=True, price=100.0, currency="EUR", name="Cached Co"),
    )
    resolver_calls = []

    result = _cached_resolve("AAA", lambda: resolver_calls.append(1) or {"price": 999.0, "currency": "USD"})

    assert result == {"price": 100.0, "currency": "EUR", "name": "Cached Co"}
    assert resolver_calls == []  # cache hit -- the live resolver must never run


def test_cached_resolve_returns_none_for_cached_negative_result(monkeypatch):
    # A symbol that failed every suffix lookup last time (e.g. SAAB) shouldn't redo all
    # five failed lookups again within the cache window.
    monkeypatch.setattr(
        portfolio_risk, "get_fresh_quote_cache", lambda symbol, max_age_seconds: SimpleNamespace(found=False)
    )
    resolver_calls = []

    result = _cached_resolve("SAAB", lambda: resolver_calls.append(1))

    assert result is None
    assert resolver_calls == []


def test_cached_resolve_calls_resolver_and_stores_result_on_cache_miss(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "get_fresh_quote_cache", lambda symbol, max_age_seconds: None)
    stored = {}
    monkeypatch.setattr(portfolio_risk, "upsert_quote_cache", lambda **kwargs: stored.update(kwargs))

    result = _cached_resolve("AAA", lambda: {"price": 50.0, "currency": "EUR", "name": "Fresh Co"})

    assert result == {"price": 50.0, "currency": "EUR", "name": "Fresh Co"}
    assert stored == {"symbol": "AAA", "found": True, "price": 50.0, "currency": "EUR", "name": "Fresh Co"}


def test_cached_resolve_stores_negative_result_on_miss(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "get_fresh_quote_cache", lambda symbol, max_age_seconds: None)
    stored = {}
    monkeypatch.setattr(portfolio_risk, "upsert_quote_cache", lambda **kwargs: stored.update(kwargs))

    result = _cached_resolve("SAAB", lambda: None)

    assert result is None
    assert stored == {"symbol": "SAAB", "found": False}
