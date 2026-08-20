import json
from types import SimpleNamespace

from app.reports import _fmt_pct, _opportunity_row_text, _suggested_size_pct, _top


def _opp(symbol, confidence, display_name="Name", thesis="Some thesis text.", raw_metrics_json=None):
    return SimpleNamespace(
        symbol=symbol, confidence=confidence, display_name=display_name, thesis=thesis, raw_metrics_json=raw_metrics_json
    )


def test_top_sorts_by_confidence_descending():
    opps = [_opp("A", 0.4), _opp("B", 0.7), _opp("C", 0.5)]
    result = _top(opps, n=2)
    assert [o.symbol for o in result] == ["B", "C"]


def test_top_respects_n():
    opps = [_opp("A", 0.9), _opp("B", 0.8), _opp("C", 0.7)]
    assert len(_top(opps, n=1)) == 1


def test_top_handles_fewer_than_n():
    opps = [_opp("A", 0.5)]
    assert len(_top(opps, n=5)) == 1


def test_fmt_pct_basic():
    assert _fmt_pct(0.1234, 2) == "12.34%"


def test_fmt_pct_none_returns_na():
    assert _fmt_pct(None) == "n/a"


def test_opportunity_row_text_truncates_long_thesis():
    long_thesis = "x" * 300
    o = _opp("AAPL", 0.6, "Apple Inc.", long_thesis)
    row = _opportunity_row_text(o)
    assert row.startswith("- AAPL (Apple Inc.) [60%]:")
    assert len(row) < 300


def test_suggested_size_pct_present():
    o = _opp("AAPL", 0.6, raw_metrics_json=json.dumps({"suggested_position_pct": 0.12}))
    assert _suggested_size_pct(o) == " | suggested size 12.0%"


def test_suggested_size_pct_missing_returns_empty():
    o = _opp("AAPL", 0.6, raw_metrics_json=json.dumps({"some_other_key": 1}))
    assert _suggested_size_pct(o) == ""


def test_suggested_size_pct_no_raw_metrics_returns_empty():
    o = _opp("AAPL", 0.6, raw_metrics_json=None)
    assert _suggested_size_pct(o) == ""
