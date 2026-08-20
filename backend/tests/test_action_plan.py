import json
from types import SimpleNamespace

from app.action_plan import _correlated_holdings_for_candidates, build_today_actions
from app.watchdog import PRICE_MOVE_FLAG_THRESHOLD


def _opp(symbol, suggested_pct, confidence=0.5, thesis="test thesis", price=None, dcf_fair_value=None):
    raw_metrics = {"suggested_position_pct": suggested_pct, "metrics": {"price": price, "dcf_fair_value": dcf_fair_value}}
    return SimpleNamespace(
        symbol=symbol,
        sleeve="satellite",
        display_name=symbol,
        confidence=confidence,
        thesis=thesis,
        raw_metrics_json=json.dumps(raw_metrics),
    )


def _priced(symbol, value_eur, name=None):
    return [{"symbol": symbol, "name": name, "quantity": 1, "value_eur": value_eur, "source": "ibkr"}]


def _filler(value_eur):
    # Padding to reach a realistic NAV, spread across enough symbols that none of them
    # trips the 25% concentration threshold and gets picked up as its own trim_unmanaged
    # action -- these tests aren't about concentration, so keep it a non-event here.
    each = value_eur / 5
    return [h for i in range(5) for h in _priced(f"ZZZ{i}", each)]


def test_new_entry_when_not_held():
    opp = _opp("AAA", suggested_pct=0.05)
    result = build_today_actions(
        opportunities=[opp], priced=_filler(10_000), macro_snapshot={}, price_moves=[], correlated_holdings={}
    )
    assert result["actions"][0]["action"] == "new_entry"
    assert result["actions"][0]["bucket"] == "buy"
    assert result["actions"][0]["suggested_eur"] == 500.0


def test_add_when_underweight_beyond_tolerance():
    opp = _opp("AAA", suggested_pct=0.05)
    priced = _priced("AAA", 100) + _filler(9_900)  # nav=10,000, held=100, suggested=500
    result = build_today_actions(opportunities=[opp], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    assert result["actions"][0]["action"] == "add"
    assert result["actions"][0]["bucket"] == "buy"


def test_trim_when_overweight_beyond_tolerance():
    opp = _opp("AAA", suggested_pct=0.05)
    priced = _priced("AAA", 900) + _filler(9_100)  # nav=10,000, held=900, suggested=500
    result = build_today_actions(opportunities=[opp], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    assert result["actions"][0]["action"] == "trim"
    assert result["actions"][0]["bucket"] == "sell"


def test_hold_within_tolerance_is_shown_not_dropped():
    opp = _opp("AAA", suggested_pct=0.05, price=100.0)
    # ZZZ has no opportunity, so it's a legitimate hold_unmanaged of its own -- only assert
    # on AAA, the symbol actually under test here.
    priced = _priced("AAA", 480) + _filler(9_520)  # nav=10,000, held=480 vs suggested=500 -- <€50 gap
    result = build_today_actions(opportunities=[opp], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    aaa = next(a for a in result["actions"] if a["symbol"] == "AAA")
    assert aaa["action"] == "hold"
    assert aaa["bucket"] == "hold"
    assert aaa["fee"] is None  # no trade suggested -- no fee to estimate


def test_not_held_and_below_actionable_threshold_is_excluded():
    # Not held, and the suggested size is too small to matter -- genuinely nothing to say,
    # unlike the in-tolerance-but-held case above which becomes a "hold".
    opp = _opp("AAA", suggested_pct=0.001)
    priced = _filler(10_000)  # suggested = €10, well under MIN_ACTIONABLE_EUR
    result = build_today_actions(opportunities=[opp], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    assert all(a["symbol"] != "AAA" for a in result["actions"])


def test_exit_when_held_and_invalidated_regardless_of_sizing():
    opp = _opp("AAA", suggested_pct=0.10, price=100.0)
    priced = _priced("AAA", 500) + _filler(9_500)
    result = build_today_actions(
        opportunities=[opp],
        priced=priced,
        macro_snapshot={},
        price_moves=[{"symbol": "AAA", "move": -0.1, "current_price": 90.0}],
        correlated_holdings={},
    )
    assert result["actions"][0]["action"] == "exit"
    assert result["actions"][0]["bucket"] == "sell"


def test_urgent_flag_true_for_exit_false_for_routine_actions():
    # The consolidated "needs attention" dashboard section filters on this field --
    # exit (invalidated thesis) is urgent, but a routine trim/add/hold/new_entry is
    # ordinary portfolio management, not something requiring same-day attention.
    exit_opp = _opp("AAA", suggested_pct=0.10, price=100.0)
    add_opp = _opp("BBB", suggested_pct=0.05)
    priced = _priced("AAA", 500) + _filler(9_500)
    result = build_today_actions(
        opportunities=[exit_opp, add_opp],
        priced=priced,
        macro_snapshot={},
        price_moves=[{"symbol": "AAA", "move": -0.1, "current_price": 90.0}],
        correlated_holdings={},
    )
    by_symbol = {a["symbol"]: a for a in result["actions"]}
    assert by_symbol["AAA"]["action"] == "exit"
    assert by_symbol["AAA"]["urgent"] is True
    assert by_symbol["BBB"]["action"] == "new_entry"
    assert by_symbol["BBB"]["urgent"] is False


def test_urgent_flag_true_for_trim_unmanaged_false_for_hold_unmanaged():
    priced = _priced("IWDA", 6_000, name="iShares Core MSCI World UCITS ETF") + _priced(
        "EIMI", 1_000, name="iShares Core MSCI EM IMI UCITS ETF"
    ) + _filler(3_000)  # IWDA 60% of NAV (over threshold), EIMI 10% (under)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    by_symbol = {a["symbol"]: a for a in result["actions"]}
    assert by_symbol["IWDA"]["action"] == "trim_unmanaged"
    assert by_symbol["IWDA"]["urgent"] is True
    assert by_symbol["EIMI"]["action"] == "hold_unmanaged"
    assert by_symbol["EIMI"]["urgent"] is False


def test_zero_nav_degrades_to_no_actionable_sizing():
    # Nothing priced (no IBKR connection, no manual holdings) -- must not fabricate a EUR
    # amount out of thin air, must just skip sizing-based actions.
    opp = _opp("AAA", suggested_pct=0.05)
    result = build_today_actions(opportunities=[opp], priced=[], macro_snapshot={}, price_moves=[], correlated_holdings={})
    assert result["actions"] == []
    assert result["nav_eur"] == 0.0


def test_actions_sorted_exit_before_add():
    exit_opp = _opp("BBB", suggested_pct=0.10, confidence=0.9, price=50.0)
    add_opp = _opp("AAA", suggested_pct=0.05, confidence=0.9)
    priced = _priced("BBB", 500) + _priced("AAA", 100) + _filler(9_400)
    result = build_today_actions(
        opportunities=[add_opp, exit_opp],
        priced=priced,
        macro_snapshot={},
        price_moves=[{"symbol": "BBB", "move": -0.1, "current_price": 45.0}],
        correlated_holdings={},
    )
    # ZZZ (padding, uncovered) legitimately sorts last as hold_unmanaged -- only the
    # ordering of the two actionable items (exit before add) is under test here.
    tradeable = [a["action"] for a in result["actions"] if a["bucket"] != "hold"]
    assert tradeable == ["exit", "add"]


def test_hold_unmanaged_for_holding_with_no_opportunity():
    # Held, but nothing in the current scan universe has a thesis on it (e.g. a European
    # ETF) -- must still show up so the buy/hold/sell picture is the FULL portfolio, not
    # just what the model has an opinion on. Padded so SAAB isn't itself
    # over-concentrated -- that path is tested separately.
    priced = _priced("SAAB", 1_000, name="Saab AB") + _filler(9_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "SAAB")
    assert entry["action"] == "hold_unmanaged"
    assert entry["bucket"] == "hold"
    assert entry["held_eur"] == 1000.0
    assert entry["confidence"] is None
    assert entry["reference_price"] is None
    assert entry["display_name"] == "Saab AB"  # resolved name, not the bare ticker


def test_hold_unmanaged_falls_back_to_symbol_when_name_unresolved():
    priced = _priced("SAAB", 1_000, name=None) + _filler(9_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "SAAB")
    assert entry["display_name"] == "SAAB"


def test_satellite_buy_gets_limit_below_reference_and_stop():
    opp = _opp("AAA", suggested_pct=0.05, price=100.0)
    result = build_today_actions(
        opportunities=[opp], priced=_filler(10_000), macro_snapshot={}, price_moves=[], correlated_holdings={}
    )
    a = result["actions"][0]
    assert a["reference_price"] == 100.0
    assert a["limit_price"] < 100.0  # buy limit sits below the reference price, not above
    assert round(a["stop_price"], 2) == round(100.0 * (1 - PRICE_MOVE_FLAG_THRESHOLD), 2)
    assert a["stop_condition"] is None  # always None now -- see _price_levels' docstring


def test_satellite_target_price_passes_through_dcf_fair_value_when_available():
    opp = _opp("AAA", suggested_pct=0.05, price=100.0, dcf_fair_value=130.0)
    result = build_today_actions(
        opportunities=[opp], priced=_filler(10_000), macro_snapshot={}, price_moves=[], correlated_holdings={}
    )
    assert result["actions"][0]["target_price"] == 130.0


def test_target_price_absent_when_dcf_not_computable():
    opp = _opp("AAA", suggested_pct=0.05, price=100.0, dcf_fair_value=None)
    result = build_today_actions(
        opportunities=[opp], priced=_filler(10_000), macro_snapshot={}, price_moves=[], correlated_holdings={}
    )
    assert result["actions"][0]["target_price"] is None


def test_exit_limit_is_at_reference_price_not_buffered():
    # An exit is urgent (thesis invalidated) -- the point is getting out, not optimizing a
    # few bps, so the limit reference sits at-market rather than a buy/trim-style buffer.
    opp = _opp("AAA", suggested_pct=0.10, price=100.0)
    priced = _priced("AAA", 500) + _filler(9_500)
    result = build_today_actions(
        opportunities=[opp],
        priced=priced,
        macro_snapshot={},
        price_moves=[{"symbol": "AAA", "move": -0.1, "current_price": 90.0}],
        correlated_holdings={},
    )
    a = result["actions"][0]
    assert a["action"] == "exit"
    assert a["limit_price"] == 100.0


def test_concentrated_uncovered_holding_flagged_as_trim_unmanaged():
    # The actual bug report this fixes: a holding outside the scan universe (e.g. a
    # European ETF) can never get a normal "trim" since no thesis exists for it, but it
    # can still legitimately be over-concentrated -- must be flagged on that basis alone,
    # in the sell bucket, not silently stuck in "hold" forever regardless of how lopsided
    # the portfolio is.
    priced = _priced("IWDA", 6_000, name="iShares Core MSCI World UCITS ETF") + _filler(4_000)  # 60% of NAV
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "IWDA")
    assert entry["action"] == "trim_unmanaged"
    assert entry["bucket"] == "sell"
    assert entry["held_eur"] == 6000.0
    assert entry["suggested_eur"] == 2500.0  # 25% of 10,000 NAV -- the target weight
    assert entry["fee"] is not None
    assert "60%" in entry["rationale"]
    assert "concentrat" in entry["rationale"].lower()


def test_concentrated_uncovered_holding_infers_accumulating_etf_tob_from_name():
    priced = _priced("IWDA", 6_000, name="iShares Core MSCI World UCITS ETF USD (Acc)") + _filler(4_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "IWDA")
    from app.calculators.fees import estimate_tob

    expected_tob = round(estimate_tob(entry["held_eur"] - entry["suggested_eur"], "etf_accumulating"), 2)
    assert entry["fee"]["tob_eur"] == expected_tob


def test_uncovered_holding_below_concentration_threshold_stays_plain_hold():
    priced = _priced("EIMI", 1_000, name="iShares Core MSCI EM IMI UCITS ETF") + _filler(9_000)  # 10% of NAV
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "EIMI")
    assert entry["action"] == "hold_unmanaged"
    assert entry["bucket"] == "hold"


def test_concentration_trim_below_min_actionable_stays_hold():
    # Just barely over the 25% threshold -- the resulting trim (30 EUR) is under
    # MIN_ACTIONABLE_EUR (50), so this should stay a plain hold rather than flag a token trim.
    priced = _priced("IWDA", 2_530, name="iShares Core MSCI World UCITS ETF") + _filler(7_470)  # 25.3% of NAV
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "IWDA")
    assert entry["action"] == "hold_unmanaged"


_MACRO = {
    "fed_funds_rate": {"value": 3.63, "date": "2026-08-14"},
    "ten_year_yield": {"value": 4.68, "date": "2026-08-14"},
    "unemployment_rate": {"value": 4.1, "date": "2026-07-01"},
}


def test_asset_class_bond_fund_gets_duration_macro_context():
    priced = _priced("AGGH", 1_000, name="iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)") + _filler(9_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot=_MACRO, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "AGGH")
    assert entry["asset_class"] == "bond"
    assert "10Y yield 4.68%" in entry["rationale"]
    assert "duration risk" in entry["rationale"].lower()
    assert "forecast" in entry["rationale"].lower()  # must explicitly disclaim it's not a prediction


def test_asset_class_equity_etf_gets_backdrop_not_duration_language():
    priced = _priced("IWDA", 1_000, name="iShares Core MSCI World UCITS ETF") + _filler(9_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot=_MACRO, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "IWDA")
    assert entry["asset_class"] == "equity"
    assert "Macro backdrop" in entry["rationale"]
    assert "duration" not in entry["rationale"].lower()  # bond-specific mechanic, must not leak into equity


def test_no_macro_context_appended_when_macro_snapshot_empty():
    # FRED_API_KEY not configured -- must not fabricate macro commentary out of nothing.
    priced = _priced("AGGH", 1_000, name="iShares Core Global Aggregate Bond UCITS ETF") + _filler(9_000)
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    entry = next(a for a in result["actions"] if a["symbol"] == "AGGH")
    assert "Macro" not in entry["rationale"]


def test_covered_opportunity_gets_equity_asset_class_from_name():
    opp = _opp("AAPL", suggested_pct=0.05, price=100.0)
    result = build_today_actions(
        opportunities=[opp], priced=_filler(10_000), macro_snapshot={}, price_moves=[], correlated_holdings={}
    )
    assert result["actions"][0]["asset_class"] == "equity"


def test_correlated_holdings_for_candidates_flags_high_correlation(monkeypatch):
    # IWDA is the portfolio's only holding (100% of NAV, well within the top-5-by-weight
    # check), AAPL is a satellite buy candidate -- a mocked correlation_analysis stands in
    # for the real yfinance-backed one so this stays a fast, deterministic unit test.
    import app.action_plan as action_plan_module

    def fake_correlation_analysis(symbols):
        assert sorted(symbols) == ["AAPL", "IWDA"]
        return {
            "symbols_analyzed": symbols,
            "symbols_excluded_no_history": [],
            "pairs": [{"symbol_a": "AAPL", "symbol_b": "IWDA", "correlation": 0.85}],
            "avg_pairwise_correlation": 0.85,
            "highly_correlated_pairs": [],
        }

    monkeypatch.setattr(action_plan_module, "correlation_analysis", fake_correlation_analysis)

    priced = _priced("IWDA", 10_000, name="iShares Core MSCI World UCITS ETF")
    result = _correlated_holdings_for_candidates(["AAPL"], priced, nav_eur=10_000)

    assert result == {"AAPL": [{"symbol": "IWDA", "correlation": 0.85, "weight": 1.0}]}


def test_correlated_holdings_for_candidates_empty_below_threshold(monkeypatch):
    import app.action_plan as action_plan_module

    def fake_correlation_analysis(symbols):
        return {
            "symbols_analyzed": symbols,
            "symbols_excluded_no_history": [],
            "pairs": [{"symbol_a": "AAPL", "symbol_b": "IWDA", "correlation": 0.3}],
            "avg_pairwise_correlation": 0.3,
            "highly_correlated_pairs": [],
        }

    monkeypatch.setattr(action_plan_module, "correlation_analysis", fake_correlation_analysis)

    priced = _priced("IWDA", 10_000, name="iShares Core MSCI World UCITS ETF")
    result = _correlated_holdings_for_candidates(["AAPL"], priced, nav_eur=10_000)

    assert result == {}


def test_correlated_holdings_for_candidates_short_circuits_with_no_candidates_or_nav():
    # Must not even attempt the live correlation_analysis call when there's nothing to
    # check -- no candidates, or a zero/negative NAV (nothing priced yet).
    assert _correlated_holdings_for_candidates([], _priced("IWDA", 10_000), nav_eur=10_000) == {}
    assert _correlated_holdings_for_candidates(["AAPL"], _priced("IWDA", 10_000), nav_eur=0) == {}


def test_build_today_actions_enriches_correlated_satellite_buy():
    opp = _opp("AAPL", suggested_pct=0.05, price=100.0)
    correlated = {"AAPL": [{"symbol": "IWDA", "correlation": 0.85, "weight": 0.6}]}
    result = build_today_actions(
        opportunities=[opp],
        priced=_filler(10_000),
        macro_snapshot={},
        price_moves=[],
        correlated_holdings=correlated,
    )
    a = result["actions"][0]
    assert a["correlated_holdings"] == [{"symbol": "IWDA", "correlation": 0.85, "weight": 0.6}]
    assert "Correlation check" in a["rationale"]
    assert "IWDA" in a["rationale"]


def test_build_today_actions_no_correlation_leaves_rationale_untouched():
    opp = _opp("AAPL", suggested_pct=0.05, price=100.0)
    result = build_today_actions(
        opportunities=[opp],
        priced=_filler(10_000),
        macro_snapshot={},
        price_moves=[],
        correlated_holdings={},
    )
    a = result["actions"][0]
    assert a["correlated_holdings"] == []
    assert "Correlation check" not in a["rationale"]


def test_asset_allocation_breaks_down_priced_portfolio_by_class():
    priced = (
        _priced("AGGH", 3_000, name="iShares Core Global Aggregate Bond UCITS ETF")
        + _priced("IWDA", 6_000, name="iShares Core MSCI World UCITS ETF")
        + _priced("PHAU", 1_000, name="WisdomTree Physical Gold")
    )
    result = build_today_actions(opportunities=[], priced=priced, macro_snapshot={}, price_moves=[], correlated_holdings={})
    alloc = result["asset_allocation"]
    assert alloc["total_value_eur"] == 10_000.0
    assert alloc["weights"] == {"bond": 0.3, "equity": 0.6, "commodity": 0.1}
