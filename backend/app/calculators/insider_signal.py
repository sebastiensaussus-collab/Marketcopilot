"""Aggregates insider Form 4 open-market transactions into a cluster-buying signal.
Cluster buying -- multiple distinct insiders independently buying in the same window --
is one of the more consistently documented signals in the academic insider-trading
literature, more so than any single insider's transaction viewed alone (one person's buy
could be idiosyncratic; several people buying independently is harder to dismiss).
"""

MIN_DISTINCT_BUYERS_FOR_CLUSTER = 2


def summarize(transactions: list[dict]) -> dict:
    buys = [t for t in transactions if t["transaction_code"] == "P"]
    sells = [t for t in transactions if t["transaction_code"] == "S"]

    distinct_buyers = {t["owner_name"] for t in buys}
    distinct_sellers = {t["owner_name"] for t in sells}

    buy_value = sum(t["shares"] * t["price_per_share"] for t in buys)
    sell_value = sum(t["shares"] * t["price_per_share"] for t in sells)

    return {
        "num_buy_transactions": len(buys),
        "num_sell_transactions": len(sells),
        "num_distinct_buyers": len(distinct_buyers),
        "num_distinct_sellers": len(distinct_sellers),
        "buy_value_usd": round(buy_value, 2),
        "sell_value_usd": round(sell_value, 2),
        "net_value_usd": round(buy_value - sell_value, 2),
        "cluster_buy_signal": len(distinct_buyers) >= MIN_DISTINCT_BUYERS_FOR_CLUSTER
        and buy_value > sell_value,
    }
