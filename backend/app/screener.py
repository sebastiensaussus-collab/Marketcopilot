"""Scans the free-data universe and ranks candidates cheaply, so the expensive model
synthesis call in synthesis.py only ever runs on a short, pre-filtered list.
"""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from app.calculators import dcf, risk, technicals
from app.calculators import fundamentals as fundamentals_calc
from app.calculators import insider_signal
from app.connectors import sec_edgar, yfinance_client
from app.settings import settings
from app.store import get_fresh_insider_signal_cache, upsert_insider_signal_cache
from app.universe import SP100

# Bounded low to keep the per-ticker SEC round trip fast enough for a live scan (each
# filing needs 2 requests: resolve the real filename, then fetch it) -- Form 4s must be
# filed within 2 business days of a transaction, so even this few filings back reliably
# covers the last several weeks for an actively-traded name. Daily-cached regardless.
INSIDER_LOOKBACK_FILINGS = 8

# Each ticker in scan_equities now does several sequential-feeling I/O calls (yfinance
# history + fundamentals, plus up to INSIDER_LOOKBACK_FILINGS*2 SEC requests) -- run
# tickers concurrently rather than one at a time. Capped well under SEC's documented
# fair-access threshold even with several tickers' SEC calls landing at once.
EQUITY_SCAN_CONCURRENCY = 8

# DCF assumptions for the fair-value sanity check (app/calculators/dcf.py) -- a single
# growth-then-terminal model, not a precision valuation. Growth is read from yfinance's
# trailing revenue growth when available, capped both ends since a single noisy metric
# shouldn't be allowed to blow up a 5-year projection; falls back to a conservative flat
# assumption when the field is missing (common for smaller/newer names).
DCF_DISCOUNT_RATE = 0.09
DCF_TERMINAL_GROWTH = 0.025
DCF_DEFAULT_GROWTH = 0.04
DCF_GROWTH_FLOOR = 0.0
DCF_GROWTH_CAP = 0.20


def _insider_stats(ticker: str) -> dict:
    """Insider cluster-buying signal for ticker, cached ~24h. Never raises -- a SEC
    hiccup for one ticker degrades to "no signal" rather than breaking the whole scan.
    """
    cached = get_fresh_insider_signal_cache(ticker)
    if cached:
        return {
            "num_buy_transactions": cached.num_buy_transactions,
            "num_sell_transactions": cached.num_sell_transactions,
            "num_distinct_buyers": cached.num_distinct_buyers,
            "num_distinct_sellers": cached.num_distinct_sellers,
            "net_value_usd": cached.net_value_usd,
            "cluster_buy_signal": cached.cluster_buy_signal,
        }

    try:
        transactions = sec_edgar.get_insider_transactions(ticker, lookback_filings=INSIDER_LOOKBACK_FILINGS)
    except Exception:
        transactions = None

    if transactions is None:
        return insider_signal.summarize([])

    summary = insider_signal.summarize(transactions)
    upsert_insider_signal_cache(symbol=ticker, summary=summary)
    return summary


def _dcf_metrics(fundamentals: dict) -> dict:
    """Fair-value sanity check via app/calculators/dcf.py -- None on both keys when FCF
    or share count is missing, which is common and not an error (small/newer names often
    don't have a positive trailing FCF yfinance will report)."""
    growth = fundamentals.get("revenue_growth")
    growth = DCF_DEFAULT_GROWTH if growth is None else max(DCF_GROWTH_FLOOR, min(growth, DCF_GROWTH_CAP))

    fair_value = dcf.fair_value_per_share(
        free_cashflow=fundamentals.get("free_cashflow"),
        shares_outstanding=fundamentals.get("shares_outstanding"),
        growth_rate=growth,
        discount_rate=DCF_DISCOUNT_RATE,
        terminal_growth_rate=DCF_TERMINAL_GROWTH,
    )
    return {
        "dcf_fair_value": fair_value,
        "dcf_margin_of_safety_pct": dcf.margin_of_safety(fair_value, fundamentals.get("price")),
    }


def _score_equity_candidate(ticker: str, benchmark_close) -> dict | None:
    """None on missing/insufficient data (expected, common) or on any unexpected error
    (e.g. a malformed fundamentals payload for an unusual ticker) -- one bad ticker must
    never take down the whole concurrent scan, since that scan feeds the unattended
    scheduled morning report."""
    try:
        hist = yfinance_client.get_price_history(ticker)
        if hist is None or len(hist) < 30:
            return None
        fundamentals = yfinance_client.get_fundamentals(ticker)
        if not fundamentals:
            return None

        close = hist["Close"]
        metrics = {
            **technicals.latest_snapshot(close),
            **fundamentals_calc.summarize(fundamentals),
            **_dcf_metrics(fundamentals),
            "sharpe_ratio": risk.sharpe_ratio(close),
            "max_drawdown": risk.max_drawdown(close),
            "correlation_spy": risk.correlation(close, benchmark_close) if benchmark_close is not None else None,
            "price": fundamentals["price"],
            "insider": _insider_stats(ticker),
        }

        return {
            "ticker": ticker,
            "name": fundamentals["name"],
            "metrics": metrics,
            "score": _equity_screen_score(metrics),
        }
    except Exception:
        logging.getLogger("market_copilot.screener").exception("Equity screening errored for %s", ticker)
        return None


def scan_equities(benchmark_ticker: str = "SPY") -> list[dict]:
    """Satellite sleeve candidates: equities ranked by a cheap composite heuristic."""
    benchmark_hist = yfinance_client.get_price_history(benchmark_ticker)
    benchmark_close = benchmark_hist["Close"] if benchmark_hist is not None else None

    universe = SP100[: settings.equity_universe_size]

    with ThreadPoolExecutor(max_workers=EQUITY_SCAN_CONCURRENCY) as pool:
        futures = [pool.submit(_score_equity_candidate, ticker, benchmark_close) for ticker in universe]
        results = [f.result() for f in futures]
        scored = [r for r in results if r is not None]

    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[: settings.equity_shortlist_size]


# The RSI-band/off-high/Sharpe weights below ARE backtestable -- app/backtest/
# equity_momentum.py walk-forward tests this exact combination against realized
# SPY-relative returns, and sweep_technical_weights() there searches nearby alternatives.
# FCF_YIELD_WEIGHT and INSIDER_SIGNAL_WEIGHT are NOT backtestable with data this app has
# access to: yfinance only exposes current-snapshot fundamentals, not point-in-time
# historical ones, so scoring a past date with today's P/E (or today's insider filings)
# would be look-ahead bias. Kept as differentiated signals on top of the validated
# technical score, not proven predictors -- see equity_momentum.py's docstring.
TECHNICAL_SCORE_WEIGHTS = {"rsi_band": 1.0, "off_high": 1.0, "sharpe": 0.5}
FCF_YIELD_WEIGHT = 10.0
INSIDER_SIGNAL_WEIGHT = 1.5


def _equity_screen_score(metrics: dict, technical_weights: dict | None = None) -> float:
    """Cheap composite heuristic used only to shortlist candidates before spending an
    LLM call on them — not a rigorous alpha model. Favors cash-generative names that are
    somewhat oversold and have decent risk-adjusted momentum, with a boost for a genuine
    insider cluster-buying signal (multiple distinct insiders buying on the open market,
    real money outweighing sells -- see app/calculators/insider_signal.py).

    technical_weights overrides TECHNICAL_SCORE_WEIGHTS for the three backtestable terms
    only -- used by equity_momentum.py's weight sweep to re-score the same metrics under
    different weight combinations without touching the two unvalidated terms.
    """
    weights = technical_weights or TECHNICAL_SCORE_WEIGHTS
    score = 0.0
    if metrics.get("fcf_yield"):
        score += metrics["fcf_yield"] * FCF_YIELD_WEIGHT
    rsi_14 = metrics.get("rsi_14")
    if rsi_14 is not None and 25 < rsi_14 < 45:
        score += weights["rsi_band"]
    pct_off_high = metrics.get("pct_off_52w_high")
    if pct_off_high is not None and pct_off_high < -0.15:
        score += weights["off_high"]
    if metrics.get("sharpe_ratio"):
        score += max(metrics["sharpe_ratio"], 0) * weights["sharpe"]
    if metrics.get("insider", {}).get("cluster_buy_signal"):
        score += INSIDER_SIGNAL_WEIGHT
    return score


def content_hash(payload: dict) -> str:
    """Stable hash of a synthesis input bundle, used to skip redundant model calls
    when nothing material has changed since the last run."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
