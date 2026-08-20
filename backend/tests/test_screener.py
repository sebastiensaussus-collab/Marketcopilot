from app import screener


def test_dcf_metrics_uses_capped_revenue_growth():
    fundamentals = {
        "free_cashflow": 1_000_000_000,
        "shares_outstanding": 100_000_000,
        "revenue_growth": 5.0,  # absurd/noisy value -- must be capped, not fed straight into a 5yr projection
        "price": 50.0,
    }
    result = screener._dcf_metrics(fundamentals)
    uncapped = screener.dcf.fair_value_per_share(
        free_cashflow=1_000_000_000,
        shares_outstanding=100_000_000,
        growth_rate=5.0,
        discount_rate=screener.DCF_DISCOUNT_RATE,
        terminal_growth_rate=screener.DCF_TERMINAL_GROWTH,
    )
    assert result["dcf_fair_value"] != uncapped
    assert result["dcf_fair_value"] > 0


def test_dcf_metrics_falls_back_to_default_growth_when_missing():
    fundamentals = {"free_cashflow": 1_000_000_000, "shares_outstanding": 100_000_000, "price": 50.0}
    result = screener._dcf_metrics(fundamentals)
    assert result["dcf_fair_value"] is not None
    assert result["dcf_margin_of_safety_pct"] is not None


def test_dcf_metrics_none_when_fcf_missing():
    fundamentals = {"free_cashflow": None, "shares_outstanding": 100_000_000, "price": 50.0}
    result = screener._dcf_metrics(fundamentals)
    assert result["dcf_fair_value"] is None
    assert result["dcf_margin_of_safety_pct"] is None


def test_equity_screen_score_defaults_match_technical_score_weights():
    # No explicit technical_weights -- must score identically to the live TECHNICAL_SCORE_WEIGHTS
    # constant, since that's the whole point of it being the default rather than a
    # separate hardcoded literal.
    metrics = {"rsi_14": 30, "pct_off_52w_high": -0.2, "sharpe_ratio": 1.0}
    default_score = screener._equity_screen_score(metrics)
    explicit_score = screener._equity_screen_score(metrics, screener.TECHNICAL_SCORE_WEIGHTS)
    assert default_score == explicit_score
    assert default_score == 1.0 + 1.0 + 1.0 * 0.5  # rsi_band + off_high + sharpe*weight


def test_equity_screen_score_technical_weights_override_changes_score():
    metrics = {"rsi_14": 30, "pct_off_52w_high": -0.2, "sharpe_ratio": 1.0}
    heavier = screener._equity_screen_score(metrics, {"rsi_band": 2.0, "off_high": 2.0, "sharpe": 1.0})
    assert heavier == 2.0 + 2.0 + 1.0 * 1.0
    assert heavier != screener._equity_screen_score(metrics)


def test_equity_screen_score_fcf_and_insider_unaffected_by_technical_weights():
    # FCF yield / insider signal aren't part of the sweep -- overriding technical_weights
    # must never change how those two terms contribute (see FCF_YIELD_WEIGHT/
    # INSIDER_SIGNAL_WEIGHT's docstring in app/screener.py for why they can't be
    # backtested and so are deliberately excluded from the weight search).
    metrics = {"fcf_yield": 0.05, "insider": {"cluster_buy_signal": True}}
    default_score = screener._equity_screen_score(metrics)
    swept_score = screener._equity_screen_score(metrics, {"rsi_band": 99, "off_high": 99, "sharpe": 99})
    assert default_score == swept_score == 0.05 * screener.FCF_YIELD_WEIGHT + screener.INSIDER_SIGNAL_WEIGHT


def test_score_equity_candidate_returns_none_on_unexpected_error(monkeypatch):
    # A ticker that blows up partway through (e.g. a malformed fundamentals payload)
    # must degrade to None, not raise -- scan_equities runs every ticker through a
    # ThreadPoolExecutor feeding the unattended scheduled morning report, and one bad
    # ticker must never take the whole scan down with it.
    def boom(ticker):
        raise ValueError("malformed payload")

    monkeypatch.setattr(screener.yfinance_client, "get_price_history", boom)

    result = screener._score_equity_candidate("BROKEN", benchmark_close=None)

    assert result is None
