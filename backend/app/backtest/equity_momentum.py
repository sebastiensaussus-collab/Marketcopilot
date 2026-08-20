"""Walk-forward backtest of the equity screener's technical/momentum signal.

Only tests the price-derivable component of screener._equity_screen_score (RSI band,
distance off trailing high, Sharpe) — not the fundamentals component (FCF yield).
yfinance only exposes current-snapshot fundamentals, not point-in-time historical ones,
so scoring a past date with today's P/E would be look-ahead bias. This deliberately
leaves that signal untested rather than fake-validate it; the live screener's
fundamentals weighting remains unvalidated until we have point-in-time fundamentals data.

At each rebalance date, technical metrics are computed from price history strictly up to
and including that date, so there's no look-ahead in what gets tested.
"""

import itertools

import pandas as pd

from app.calculators import risk, technicals
from app.connectors import yfinance_client
from app.screener import TECHNICAL_SCORE_WEIGHTS, _equity_screen_score

WARMUP_DAYS = 260  # > 252 trading days so a trailing-52-week high is always available
TRAILING_WINDOW = 252

# A coarse, LOCAL search around the current live weights, not an unconstrained one -- see
# sweep_technical_weights()'s docstring for why. {0.5, 1.0, 1.5} deliberately includes
# today's live values (1.0, 1.0, 0.5) as one of the 27 combinations, so "the sweep found
# nothing better nearby" is a real, checkable outcome, not just an assumption.
WEIGHT_SWEEP_VALUES = (0.5, 1.0, 1.5)


def _technical_metrics_as_of(close: pd.Series, i: int) -> dict:
    window = close.iloc[: i + 1]
    trailing_high = window.tail(TRAILING_WINDOW).max()
    trailing_year = window.tail(TRAILING_WINDOW)
    return {
        **technicals.latest_snapshot(window),
        "pct_off_52w_high": float(window.iloc[-1] / trailing_high - 1),
        "sharpe_ratio": risk.sharpe_ratio(trailing_year),
    }


def _forward_return(close: pd.Series, i: int, holding_period_days: int) -> float | None:
    j = i + holding_period_days
    if j >= len(close):
        return None
    return float(close.iloc[j] / close.iloc[i] - 1)


def _summarize(periods: list[dict], rebalance_every_days: int) -> dict:
    excess_returns = [p["excess_return"] for p in periods]
    n = len(excess_returns)
    mean_excess = sum(excess_returns) / n
    variance = sum((r - mean_excess) ** 2 for r in excess_returns) / n
    std_excess = variance**0.5
    hit_rate = sum(1 for r in excess_returns if r > 0) / n
    periods_per_year = 252 / rebalance_every_days
    sharpe = (mean_excess / std_excess * (periods_per_year**0.5)) if std_excess > 1e-9 else None

    return {
        "num_rebalance_periods": n,
        "hit_rate_vs_spy": hit_rate,
        "avg_excess_return_per_period": mean_excess,
        "cumulative_excess_return": sum(excess_returns),
        "annualized_sharpe_of_excess": sharpe,
    }


def _fetch_ticker_closes(tickers: list[str], years: int, holding_period_days: int) -> dict[str, pd.Series]:
    """The network-bound part of a walk-forward run, split out so sweep_technical_weights()
    can fetch every ticker's history ONCE and then re-score/re-summarize it many times in
    memory -- calling the old single-function run_walk_forward once per weight combination
    would have refetched the same ~100 tickers' histories per combination, and Yahoo
    Finance rate-limiting under sustained load is a real, live-verified problem this app
    has already hit, not a hypothetical to guard against speculatively.
    """
    ticker_closes = {}
    for ticker in tickers:
        hist = yfinance_client.get_price_history(ticker, period=f"{years + 1}y")
        if hist is not None and len(hist) > WARMUP_DAYS + holding_period_days:
            ticker_closes[ticker] = hist["Close"]
    return ticker_closes


def _compute_walk_forward(
    ticker_closes: dict[str, pd.Series],
    benchmark_close: pd.Series,
    holding_period_days: int,
    rebalance_every_days: int,
    top_n: int,
    technical_weights: dict | None = None,
) -> dict:
    """Pure computation core -- no network calls, so it's cheap to call many times over
    the same pre-fetched data with different technical_weights (see
    sweep_technical_weights()). Identical logic to the old single-function
    run_walk_forward, just no longer coupled to fetching.
    """
    if not ticker_closes:
        return {"error": "no ticker histories available"}

    min_len = min(len(c) for c in ticker_closes.values())
    rebalance_positions = range(WARMUP_DAYS, min_len - holding_period_days, rebalance_every_days)

    periods = []
    for i in rebalance_positions:
        scored = []
        for ticker, close in ticker_closes.items():
            fwd = _forward_return(close, i, holding_period_days)
            if fwd is None:
                continue
            metrics = _technical_metrics_as_of(close, i)
            scored.append((ticker, _equity_screen_score(metrics, technical_weights), fwd))

        bench_fwd = _forward_return(benchmark_close, min(i, len(benchmark_close) - 1), holding_period_days)
        if not scored or bench_fwd is None:
            continue

        scored.sort(key=lambda x: x[1], reverse=True)
        picks = scored[:top_n]
        strategy_return = sum(p[2] for p in picks) / len(picks)

        any_close = next(iter(ticker_closes.values()))
        periods.append(
            {
                "date": str(any_close.index[i].date()),
                "strategy_return": strategy_return,
                "benchmark_return": bench_fwd,
                "excess_return": strategy_return - bench_fwd,
                "picks": [p[0] for p in picks],
            }
        )

    if not periods:
        return {"error": "not enough history to run any rebalance periods"}

    return {
        "universe_size": len(ticker_closes),
        "holding_period_days": holding_period_days,
        **_summarize(periods, rebalance_every_days),
        "periods": periods,
    }


def run_walk_forward(
    tickers: list[str],
    years: int = 3,
    holding_period_days: int = 21,
    rebalance_every_days: int = 21,
    top_n: int = 10,
) -> dict:
    benchmark_hist = yfinance_client.get_price_history("SPY", period=f"{years + 1}y")
    if benchmark_hist is None:
        return {"error": "could not fetch SPY benchmark history"}
    benchmark_close = benchmark_hist["Close"]

    ticker_closes = _fetch_ticker_closes(tickers, years, holding_period_days)
    return _compute_walk_forward(ticker_closes, benchmark_close, holding_period_days, rebalance_every_days, top_n)


def sweep_technical_weights(
    tickers: list[str],
    years: int = 3,
    holding_period_days: int = 21,
    rebalance_every_days: int = 21,
    top_n: int = 10,
) -> dict:
    """Grid search over the three backtestable weights in TECHNICAL_SCORE_WEIGHTS
    (rsi_band, off_high, sharpe) -- the only terms of _equity_screen_score that can be
    validated against realized returns (see that constant's docstring in app/screener.py
    for why FCF yield / insider signal can't be). Fetches price histories once, then
    re-scores/re-summarizes for all 27 combinations of WEIGHT_SWEEP_VALUES purely in
    memory -- no repeated network calls.

    Deliberately a coarse LOCAL search, not an unconstrained one: with ~3 years of SP100
    history and 21-day rebalances, there are only a few dozen rebalance periods total --
    enough to sanity-check the current weights against nearby alternatives, not enough to
    support an exhaustive search without real overfitting risk. Ranked by
    annualized_sharpe_of_excess, the same risk-adjusted metric run_walk_forward already
    reports, so "current" and "best" are measured on an identical yardstick.
    """
    benchmark_hist = yfinance_client.get_price_history("SPY", period=f"{years + 1}y")
    if benchmark_hist is None:
        return {"error": "could not fetch SPY benchmark history"}
    benchmark_close = benchmark_hist["Close"]

    ticker_closes = _fetch_ticker_closes(tickers, years, holding_period_days)
    if not ticker_closes:
        return {"error": "no ticker histories available"}

    def _run(weights: dict) -> dict:
        result = _compute_walk_forward(
            ticker_closes, benchmark_close, holding_period_days, rebalance_every_days, top_n, technical_weights=weights
        )
        return {
            "weights": weights,
            "annualized_sharpe_of_excess": result.get("annualized_sharpe_of_excess"),
            "cumulative_excess_return": result.get("cumulative_excess_return"),
            "hit_rate_vs_spy": result.get("hit_rate_vs_spy"),
            "num_rebalance_periods": result.get("num_rebalance_periods"),
        }

    current_result = _run(TECHNICAL_SCORE_WEIGHTS)
    if current_result.get("num_rebalance_periods") is None:
        return {"error": "not enough history to run any rebalance periods"}

    all_results = [
        _run({"rsi_band": rsi_band, "off_high": off_high, "sharpe": sharpe})
        for rsi_band, off_high, sharpe in itertools.product(WEIGHT_SWEEP_VALUES, repeat=3)
    ]
    best_result = max(all_results, key=lambda r: r["annualized_sharpe_of_excess"] if r["annualized_sharpe_of_excess"] is not None else float("-inf"))

    return {
        "universe_size": len(ticker_closes),
        "current": current_result,
        "best": best_result,
        "all_results": all_results,
        "fundamentals_note": (
            "FCF yield and insider cluster-buy weights are not part of this search -- yfinance "
            "only exposes current-snapshot fundamentals, not point-in-time historical ones, so "
            "scoring a past rebalance date with today's fundamentals would be look-ahead bias. "
            "Only the technical component (RSI band, off-high, Sharpe) is backtestable with "
            "data this app has access to."
        ),
    }
