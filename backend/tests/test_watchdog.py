from types import SimpleNamespace

from app import watchdog


def test_price_move_check_flags_only_symbols_past_threshold(monkeypatch):
    import pandas as pd

    entries = [
        SimpleNamespace(sleeve="satellite", symbol="AAA", price_at_creation=100.0),
        SimpleNamespace(sleeve="satellite", symbol="BBB", price_at_creation=100.0),
    ]
    opportunities = [SimpleNamespace(symbol="AAA"), SimpleNamespace(symbol="BBB")]
    prices = {"AAA": 101.0, "BBB": 120.0}  # AAA: 1% move (under threshold), BBB: 20% (over)

    monkeypatch.setattr(watchdog, "get_journal_entries", lambda: entries)
    monkeypatch.setattr(watchdog, "get_opportunities", lambda sleeve: opportunities)
    monkeypatch.setattr(
        watchdog.yfinance_client,
        "get_price_history",
        lambda symbol, period="5d": pd.DataFrame({"Close": [prices[symbol]]}),
    )

    moves = watchdog.price_move_check()

    assert [m["symbol"] for m in moves] == ["BBB"]
