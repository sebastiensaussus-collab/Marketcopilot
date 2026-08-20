"""Portfolio-level risk: value-weighted concentration and correlation across everything
actually held (IBKR + manual holdings combined) -- per-position Kelly sizing (app/sizing.py)
says nothing about whether the whole book is secretly one correlated bet.

Concentration is computable for every holding: IBKR gives live prices directly, and
manual (ING/Bolero) holdings are priced via yfinance's quote endpoint plus FX conversion
to EUR (matching the account currency on both broker reports). Correlation is a harder
ask -- it needs a *historical return series*, and empirically (verified against the real
holdings in this portfolio) European UCITS ETF and Stockholm listings return a live quote
from yfinance's quote endpoint but nothing at all from `.history()`. So correlation is
only computed for symbols where a real history exists (in practice, the US-listed
equities), and everything excluded is reported explicitly rather than silently dropped.
"""

from app.calculators.risk import correlation as compute_correlation
from app.connectors import fx, ibkr, yfinance_client
from app.portfolio import get_unified_portfolio
from app.store import get_fresh_quote_cache, upsert_quote_cache

CONCENTRATION_THRESHOLD = 0.25  # flag any single symbol above this share of priced value
HIGH_CORRELATION_THRESHOLD = 0.8
ACCOUNT_CCY = "EUR"

# Fresh enough for a personal dashboard, long enough that /portfolio, /portfolio/risk, and
# /action-plan -- all fired independently on every page load -- don't each redo the same
# multi-suffix disambiguation search for the same holdings seconds apart. This was the
# actual dominant cost once a real hang-risk parallelization attempt was ruled out (see
# git history / prior session notes) -- caching removes the *repeat* work instead of
# trying to make concurrent live lookups safe.
QUOTE_CACHE_TTL_SECONDS = 300

# NOTE: tried parallelizing the manual-holdings resolution loop in priced_holdings() below,
# AND separately correlation_analysis()'s history fetches, both with a ThreadPoolExecutor
# (same pattern as app/screener.py's equity scan). Both live-verified to reproducibly HANG
# /portfolio/risk and /action-plan for tens of seconds (once measured past 60s outright)
# when called through the actual running server -- but not in a standalone script, and not
# for screener.py's own use of the identical pattern. The common thread (no pun intended):
# both call paths run on the same request thread as ibkr.fetch_positions() (an ib_async
# connect/disconnect cycle) somewhere earlier in the same request -- for
# correlation_analysis() specifically, that's build_today_actions() calling
# priced_holdings() (which touches IBKR) before later calling correlation_analysis() on
# that same thread. The working theory is an asyncio-event-loop interaction left behind on
# the thread, not a generic "ThreadPoolExecutor + yfinance" problem -- screener.py's equity
# scan never shares a thread with an IBKR call, which is presumably why it doesn't trigger
# this. Reverted both to sequential rather than ship a hang risk chasing a speed win;
# revisit by moving IBKR's connect/disconnect off the request thread instead of
# re-attempting parallelism on any thread that might have touched it.

# Best-effort international suffixes to try when a bare symbol doesn't resolve --
# common LSE/Amsterdam/Frankfurt/Stockholm conventions.
#
# Stopping at the first suffix that resolves is NOT safe on its own -- verified live
# against this portfolio's real Bolero/ING cost basis: bare "AGGH" resolves to the
# unrelated US-listed "Simplify Aggregate Bond ETF" ($19.86) while the actually-held
# LSE/Amsterdam-listed "iShares Core Global Aggregate Bond UCITS ETF" (~EUR4.90-5.21) only
# shows up under ".L"/".AS". Same collision for "WSML" ($35.93 bare vs the real UCITS
# listing under ".L"). A retail European broker's plain ticker landing on an unrelated
# US product by coincidence is a real failure mode, not a hypothetical one.
SUFFIX_CANDIDATES = ["", ".L", ".ST", ".AS", ".DE"]


def _price_in_account_ccy_for_ranking(quote: dict) -> float:
    """FX-converts a candidate quote to the account currency before it's compared to a
    cost basis -- comparing raw prices across currencies is not just imprecise, it
    actively picks the wrong candidate (caught live: NVDA's correct US listing at $224
    lost to an unrelated EUR130 Xetra listing named "61" purely because 130 happened to
    sit closer to a EUR cost basis than the unconverted 224 did). Unresolvable FX sorts
    last (never preferred) rather than crashing the comparison.
    """
    rate = fx.get_fx_rate(quote["currency"], ACCOUNT_CCY)
    return quote["price"] * rate if rate is not None else float("inf")


# How close (relative) a candidate's FX-converted price needs to be to the known cost
# basis to accept it immediately without paying for the remaining suffix lookups --
# a real position's current price vs. its cost basis can easily drift 30% without being
# a different security, so this stays loose; the point is ruling out the AGGH/WSML-style
# collisions (multiples away, not a double-digit percent away), not tight validation.
_COST_BASIS_MATCH_TOLERANCE = 0.3


def _resolve_quote(symbol: str, average_cost: float | None = None) -> dict | None:
    """Tries suffixes in order and stops at the first that resolves close enough to the
    position's real cost basis from the broker CSV import (or, with no cost basis on
    file, simply the first that resolves at all -- the old behavior, unchanged). Only
    when nothing resolves close enough does it fall back to trying every remaining
    suffix and picking whichever candidate is FX-nearest -- the disambiguation needed for
    a genuine ticker collision (see SUFFIX_CANDIDATES' docstring), paid for only when the
    fast path actually needs it, not on every single lookup.
    """
    candidates = []
    for suffix in SUFFIX_CANDIDATES:
        quote = yfinance_client.get_current_quote(f"{symbol}{suffix}")
        if not quote:
            continue
        if average_cost is None:
            return quote
        candidates.append(quote)
        converted = _price_in_account_ccy_for_ranking(quote)
        if converted != float("inf") and abs(converted - average_cost) <= _COST_BASIS_MATCH_TOLERANCE * average_cost:
            return quote

    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(_price_in_account_ccy_for_ranking(q) - average_cost))


def _value_in_account_ccy(price: float, currency: str, quantity: float) -> float | None:
    rate = fx.get_fx_rate(currency, ACCOUNT_CCY)
    if rate is None:
        return None
    return price * quantity * rate


def _cached_resolve(symbol: str, resolver) -> dict | None:
    """Cache-first wrapper around a live resolver (_resolve_quote) -- see QuoteCache's
    docstring in app/store.py for why this matters. `resolver` is a zero-arg callable so
    the (potentially expensive) live lookup only actually runs on a cache miss.
    """
    cached = get_fresh_quote_cache(symbol, max_age_seconds=QUOTE_CACHE_TTL_SECONDS)
    if cached is not None:
        if not cached.found:
            return None
        return {"price": cached.price, "currency": cached.currency, "name": cached.name}

    quote = resolver()
    if quote is None:
        upsert_quote_cache(symbol=symbol, found=False)
        return None

    upsert_quote_cache(symbol=symbol, found=True, price=quote["price"], currency=quote["currency"], name=quote.get("name"))
    return quote


def priced_holdings() -> tuple[list[dict], list[str]]:
    """Returns (priced holdings, symbols that couldn't be priced)."""
    priced = []
    unpriced = []

    ibkr_positions = ibkr.fetch_positions() or []
    for p in ibkr_positions:
        value = _value_in_account_ccy(p["market_price"], p["currency"], p["position"])
        if value is None:
            unpriced.append(p["symbol"])
            continue
        priced.append(
            {
                "symbol": p["symbol"],
                "name": p.get("name") or p["symbol"],
                "quantity": p["position"],
                "value_eur": value,
                "source": "ibkr",
            }
        )

    portfolio = get_unified_portfolio()
    for broker, holdings in portfolio["manual"].items():
        for h in holdings:
            quote = _cached_resolve(h["symbol"], lambda h=h: _resolve_quote(h["symbol"], average_cost=h.get("average_cost")))
            if quote is None:
                unpriced.append(h["symbol"])
                continue
            value = _value_in_account_ccy(quote["price"], quote["currency"], h["quantity"])
            if value is None:
                unpriced.append(h["symbol"])
                continue
            priced.append(
                {
                    "symbol": h["symbol"],
                    "name": quote.get("name") or h["symbol"],
                    "quantity": h["quantity"],
                    "value_eur": value,
                    "source": broker,
                }
            )

    return priced, sorted(set(unpriced))


def _names_by_symbol(priced: list[dict]) -> dict[str, str]:
    """First name seen per symbol wins -- a symbol should resolve to the same holding
    across sources, so any one of them is enough to label it."""
    names: dict[str, str] = {}
    for h in priced:
        names.setdefault(h["symbol"], h.get("name") or h["symbol"])
    return names


def _concentration(priced: list[dict]) -> dict:
    by_symbol: dict[str, float] = {}
    for h in priced:
        by_symbol[h["symbol"]] = by_symbol.get(h["symbol"], 0.0) + h["value_eur"]

    total = sum(by_symbol.values())
    if total <= 0:
        return {"total_value_eur": 0.0, "weights": {}, "concentration_flags": []}

    weights = {sym: round(val / total, 4) for sym, val in by_symbol.items()}
    flags = [
        {"symbol": sym, "weight": w}
        for sym, w in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
        if w > CONCENTRATION_THRESHOLD
    ]
    return {"total_value_eur": round(total, 2), "weights": weights, "concentration_flags": flags}


def _summarize_correlation_pairs(pairs: list[dict]) -> dict:
    avg_correlation = round(sum(p["correlation"] for p in pairs) / len(pairs), 3) if pairs else None
    highly_correlated = [p for p in pairs if p["correlation"] >= HIGH_CORRELATION_THRESHOLD]
    return {"avg_pairwise_correlation": avg_correlation, "highly_correlated_pairs": highly_correlated}


def correlation_analysis(symbols: list[str]) -> dict:
    histories = {}
    for symbol in symbols:
        hist = yfinance_client.get_price_history(symbol, period="6mo")
        if hist is not None and len(hist) > 30:
            histories[symbol] = hist["Close"]

    excluded = sorted(set(symbols) - set(histories.keys()))

    pairs = []
    symbols_with_history = list(histories.keys())
    for i, sym_a in enumerate(symbols_with_history):
        for sym_b in symbols_with_history[i + 1 :]:
            corr = compute_correlation(histories[sym_a], histories[sym_b])
            if corr is not None:
                pairs.append({"symbol_a": sym_a, "symbol_b": sym_b, "correlation": round(corr, 3)})

    summary = _summarize_correlation_pairs(pairs)

    return {
        "symbols_analyzed": symbols_with_history,
        "symbols_excluded_no_history": excluded,
        "pairs": pairs,
        **summary,
    }


def compute_portfolio_risk() -> dict:
    priced, unpriced = priced_holdings()
    concentration = _concentration(priced)

    unique_symbols = sorted({h["symbol"] for h in priced})
    correlation_result = correlation_analysis(unique_symbols)

    return {
        "concentration": concentration,
        "unpriced_symbols": unpriced,
        "correlation": correlation_result,
        "names": _names_by_symbol(priced),
    }
