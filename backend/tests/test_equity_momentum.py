import pandas as pd

from app.backtest import equity_momentum
from app.backtest.equity_momentum import _forward_return, _summarize


def test_forward_return_known_value():
    close = pd.Series([100.0, 102.0, 105.0, 110.0])
    result = _forward_return(close, 0, 2)
    assert round(result, 6) == round(105.0 / 100.0 - 1, 6)


def test_forward_return_out_of_bounds_returns_none():
    close = pd.Series([100.0, 102.0])
    assert _forward_return(close, 1, 5) is None


def test_summarize_known_hit_rate_and_cumulative():
    periods = [
        {"excess_return": 0.02},
        {"excess_return": -0.01},
        {"excess_return": 0.03},
        {"excess_return": -0.005},
    ]
    result = _summarize(periods, rebalance_every_days=21)

    assert result["num_rebalance_periods"] == 4
    assert result["hit_rate_vs_spy"] == 0.5
    assert round(result["cumulative_excess_return"], 6) == round(0.02 - 0.01 + 0.03 - 0.005, 6)


def _rising_close_history(ticker, period=None):
    # Long enough to clear WARMUP_DAYS + a 21-day holding period regardless of ticker.
    # A real DatetimeIndex, not the default RangeIndex -- _compute_walk_forward reads
    # .index[i].date() when building each period's record.
    dates = pd.date_range("2020-01-01", periods=320, freq="D")
    return pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(320)]}, index=dates)


def test_fetch_ticker_closes_and_sweep_only_fetch_each_ticker_once(monkeypatch):
    # The actual bug this refactor fixes: sweeping 27 weight combinations by calling the
    # old single-function run_walk_forward once per combo would have refetched every
    # ticker's price history 27 times over -- a real cost/rate-limit risk (Yahoo Finance
    # rate-limiting under sustained load is live-verified elsewhere this session, not a
    # hypothetical). Only the fetch count proves the split actually decoupled fetching
    # from scoring.
    calls = []

    def counting_fetch(ticker, period=None):
        calls.append(ticker)
        return _rising_close_history(ticker, period)

    monkeypatch.setattr(equity_momentum.yfinance_client, "get_price_history", counting_fetch)

    equity_momentum.sweep_technical_weights(["AAA", "BBB"], years=1, holding_period_days=21, rebalance_every_days=21, top_n=1)

    assert calls.count("SPY") == 1
    assert calls.count("AAA") == 1
    assert calls.count("BBB") == 1
    assert len(calls) == 3  # not 3 * 27


def test_sweep_technical_weights_selects_the_max_sharpe_combination(monkeypatch):
    monkeypatch.setattr(equity_momentum.yfinance_client, "get_price_history", _rising_close_history)

    def fake_compute(ticker_closes, benchmark_close, holding_period_days, rebalance_every_days, top_n, technical_weights=None):
        weights = technical_weights or equity_momentum.TECHNICAL_SCORE_WEIGHTS
        # A deterministic, checkable "Sharpe" per combination -- real backtest math is
        # already covered by _summarize's own tests; this isolates the sweep's
        # max-selection logic from needing realistic synthetic market data.
        fake_sharpe = weights["rsi_band"] + weights["off_high"] + weights["sharpe"]
        return {
            "annualized_sharpe_of_excess": fake_sharpe,
            "cumulative_excess_return": 0.0,
            "hit_rate_vs_spy": 0.5,
            "num_rebalance_periods": 10,
        }

    monkeypatch.setattr(equity_momentum, "_compute_walk_forward", fake_compute)

    result = equity_momentum.sweep_technical_weights(["AAA"], years=1)

    assert result["best"]["weights"] == {"rsi_band": 1.5, "off_high": 1.5, "sharpe": 1.5}
    assert result["best"]["annualized_sharpe_of_excess"] == 4.5
    assert len(result["all_results"]) == 27
    assert result["current"]["weights"] == equity_momentum.TECHNICAL_SCORE_WEIGHTS
    assert result["current"]["annualized_sharpe_of_excess"] == 2.5  # 1.0 + 1.0 + 0.5
    assert "fundamentals_note" in result
    assert "look-ahead" in result["fundamentals_note"]


def test_sweep_technical_weights_errors_when_no_ticker_histories_available(monkeypatch):
    def no_data(ticker, period=None):
        return None

    monkeypatch.setattr(equity_momentum.yfinance_client, "get_price_history", no_data)

    result = equity_momentum.sweep_technical_weights(["AAA"], years=1)

    assert "error" in result
