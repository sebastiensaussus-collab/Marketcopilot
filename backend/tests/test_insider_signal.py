from app.calculators.insider_signal import summarize


def _txn(owner, code, shares, price):
    return {
        "owner_name": owner,
        "transaction_code": code,
        "transaction_date": "2026-01-01",
        "shares": shares,
        "price_per_share": price,
    }


def test_summarize_counts_buys_and_sells_separately():
    transactions = [
        _txn("Alice", "P", 100, 10.0),
        _txn("Bob", "S", 50, 20.0),
    ]
    result = summarize(transactions)
    assert result["num_buy_transactions"] == 1
    assert result["num_sell_transactions"] == 1
    assert result["buy_value_usd"] == 1000.0
    assert result["sell_value_usd"] == 1000.0
    assert result["net_value_usd"] == 0.0


def test_summarize_distinct_buyers_deduplicates_same_person():
    transactions = [
        _txn("Alice", "P", 100, 10.0),
        _txn("Alice", "P", 50, 11.0),  # same person, second transaction
        _txn("Bob", "P", 20, 10.0),
    ]
    result = summarize(transactions)
    assert result["num_buy_transactions"] == 3
    assert result["num_distinct_buyers"] == 2


def test_cluster_buy_signal_requires_multiple_distinct_buyers():
    single_buyer = [_txn("Alice", "P", 100, 10.0)]
    result = summarize(single_buyer)
    assert result["cluster_buy_signal"] is False


def test_cluster_buy_signal_true_with_two_buyers_net_positive():
    transactions = [_txn("Alice", "P", 100, 10.0), _txn("Bob", "P", 50, 10.0)]
    result = summarize(transactions)
    assert result["num_distinct_buyers"] == 2
    assert result["cluster_buy_signal"] is True


def test_cluster_buy_signal_false_if_sells_outweigh_buys_despite_multiple_buyers():
    transactions = [
        _txn("Alice", "P", 10, 10.0),  # buy: 100
        _txn("Bob", "P", 10, 10.0),  # buy: 100
        _txn("Carol", "S", 1000, 10.0),  # sell: 10000, dwarfs the buys
    ]
    result = summarize(transactions)
    assert result["num_distinct_buyers"] == 2
    assert result["cluster_buy_signal"] is False


def test_summarize_empty_transactions():
    result = summarize([])
    assert result["num_buy_transactions"] == 0
    assert result["num_distinct_buyers"] == 0
    assert result["cluster_buy_signal"] is False
