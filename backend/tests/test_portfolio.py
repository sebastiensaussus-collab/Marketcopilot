import pytest
from types import SimpleNamespace

import app.portfolio as portfolio_module
from app.portfolio import _is_valid_extraction, available_cash_eur, compute_trade_result, parse_holdings_csv, set_broker_cash


def test_parse_holdings_csv_basic():
    content = "symbol,quantity,average_cost\nAAPL,10,150.5\nMSFT,5,300\n"
    result = parse_holdings_csv(content)

    assert result == [
        {"symbol": "AAPL", "quantity": 10.0, "average_cost": 150.5},
        {"symbol": "MSFT", "quantity": 5.0, "average_cost": 300.0},
    ]


def test_parse_holdings_csv_average_cost_optional():
    content = "symbol,quantity\nAAPL,10\n"
    result = parse_holdings_csv(content)
    assert result == [{"symbol": "AAPL", "quantity": 10.0, "average_cost": None}]


def test_parse_holdings_csv_column_order_and_case_insensitive():
    content = "Quantity,Symbol\n10,aapl\n"
    result = parse_holdings_csv(content)
    assert result == [{"symbol": "AAPL", "quantity": 10.0, "average_cost": None}]


def test_parse_holdings_csv_skips_blank_lines():
    content = "symbol,quantity\nAAPL,10\n\nMSFT,5\n"
    result = parse_holdings_csv(content)
    assert len(result) == 2


def test_parse_holdings_csv_missing_required_column_raises():
    content = "symbol,shares\nAAPL,10\n"
    with pytest.raises(ValueError, match="quantity"):
        parse_holdings_csv(content)


def test_parse_holdings_csv_empty_file_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_holdings_csv("")


def test_parse_holdings_csv_non_numeric_quantity_raises():
    content = "symbol,quantity\nAAPL,ten\n"
    with pytest.raises(ValueError, match="not a number"):
        parse_holdings_csv(content)


def test_parse_holdings_csv_missing_quantity_value_raises():
    content = "symbol,quantity\nAAPL,\n"
    with pytest.raises(ValueError, match="required"):
        parse_holdings_csv(content)


def test_compute_trade_result_buy_into_empty_position_creates_row():
    new_quantity, new_average_cost = compute_trade_result(0.0, None, "buy", 10, 100.0)
    assert new_quantity == 10
    assert new_average_cost == 100.0


def test_compute_trade_result_buy_weighted_averages_cost():
    # 10 @ 100 already held, buy 10 more @ 200 -> 20 @ 150 blended
    new_quantity, new_average_cost = compute_trade_result(10.0, 100.0, "buy", 10, 200.0)
    assert new_quantity == 20
    assert new_average_cost == 150.0


def test_compute_trade_result_sell_reduces_quantity_keeps_cost_basis():
    new_quantity, new_average_cost = compute_trade_result(10.0, 100.0, "sell", 4, 999.0)
    assert new_quantity == 6
    assert new_average_cost == 100.0  # unaffected by the sale price


def test_compute_trade_result_sell_exact_quantity_closes_position():
    new_quantity, _ = compute_trade_result(10.0, 100.0, "sell", 10, 150.0)
    assert new_quantity == 0


def test_compute_trade_result_sell_more_than_held_raises():
    with pytest.raises(ValueError, match="only 10.0 held"):
        compute_trade_result(10.0, 100.0, "sell", 11, 150.0)


def test_compute_trade_result_sell_with_nothing_held_raises():
    with pytest.raises(ValueError, match="only 0.0 held"):
        compute_trade_result(0.0, None, "sell", 1, 150.0)


def test_compute_trade_result_nonpositive_quantity_raises():
    with pytest.raises(ValueError, match="positive"):
        compute_trade_result(10.0, 100.0, "buy", 0, 150.0)


def test_compute_trade_result_unknown_action_raises():
    with pytest.raises(ValueError, match="Unknown action"):
        compute_trade_result(10.0, 100.0, "short", 1, 150.0)


def test_is_valid_extraction_accepts_well_formed_list():
    assert _is_valid_extraction(
        [{"name": "NVIDIA CORP", "symbol_guess": "NVDA", "quantity": 6, "average_cost": 144.47}]
    )


def test_is_valid_extraction_accepts_empty_list():
    assert _is_valid_extraction([])


def test_is_valid_extraction_rejects_non_list():
    assert not _is_valid_extraction({"name": "NVIDIA CORP"})


def test_is_valid_extraction_rejects_missing_required_field():
    assert not _is_valid_extraction([{"name": "NVIDIA CORP", "quantity": 6}])  # no symbol_guess


def test_is_valid_extraction_rejects_non_numeric_quantity():
    assert not _is_valid_extraction([{"name": "NVIDIA CORP", "symbol_guess": "NVDA", "quantity": "six"}])


def test_set_broker_cash_rejects_negative():
    with pytest.raises(ValueError, match="non-negative"):
        set_broker_cash("bolero", -1)


def test_available_cash_eur_combines_ibkr_and_broker_cash(monkeypatch):
    monkeypatch.setattr(
        portfolio_module.ibkr, "fetch_account_summary", lambda: {"TotalCashValue": {"value": 1000, "currency": "USD"}}
    )
    monkeypatch.setattr(portfolio_module.fx, "get_fx_rate", lambda from_ccy, to_ccy: 0.9)
    monkeypatch.setattr(
        portfolio_module, "get_broker_cash", lambda: [SimpleNamespace(broker="bolero", cash_eur=200.0)]
    )

    result = available_cash_eur()

    assert result["by_broker"] == {"ibkr": 900.0, "bolero": 200.0}
    assert result["total_eur"] == 1100.0


def test_available_cash_eur_ibkr_not_connected(monkeypatch):
    monkeypatch.setattr(portfolio_module.ibkr, "fetch_account_summary", lambda: None)
    monkeypatch.setattr(portfolio_module, "get_broker_cash", lambda: [SimpleNamespace(broker="ing", cash_eur=50.0)])

    result = available_cash_eur()

    assert "ibkr" not in result["by_broker"]
    assert result["total_eur"] == 50.0


def test_available_cash_eur_fx_unavailable_excludes_ibkr_cash(monkeypatch):
    monkeypatch.setattr(
        portfolio_module.ibkr, "fetch_account_summary", lambda: {"TotalCashValue": {"value": 1000, "currency": "USD"}}
    )
    monkeypatch.setattr(portfolio_module.fx, "get_fx_rate", lambda from_ccy, to_ccy: None)
    monkeypatch.setattr(portfolio_module, "get_broker_cash", lambda: [])

    result = available_cash_eur()

    assert "ibkr" not in result["by_broker"]
    assert result["total_eur"] == 0.0
