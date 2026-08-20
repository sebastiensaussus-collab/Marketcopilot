"""Invalidation watchdog checks, shared between app/reports.py and app/action_plan.py.

Deliberately a computable proxy, not a literal check of the free-text
invalidation_condition Claude writes (that's natural language, not machine checkable
without another LLM call per position, which would defeat the cost design elsewhere in
this app). Has price moved materially since the thesis's journal snapshot?

Extracted from reports.py so action_plan.py can reuse the same checks without the two
modules importing each other.
"""

from concurrent.futures import ThreadPoolExecutor

from app.connectors import yfinance_client
from app.store import get_journal_entries, get_opportunities

PRICE_MOVE_FLAG_THRESHOLD = 0.07  # flag if price moved >7% since thesis creation

# Runs once per *accumulated* opportunity (every symbol ever shortlisted, not just
# today's), each doing a live network call -- sequentially this was a real contributor to
# /action-plan's response time as the accumulated set grew past a couple dozen symbols.
# Parallelized the same way app/screener.py's equity scan already is, not a new pattern.
WATCHDOG_CONCURRENCY = 8


def _check_price_move(o, entry) -> dict | None:
    hist = yfinance_client.get_price_history(o.symbol, period="5d")
    if hist is None or hist.empty:
        return None
    current_price = float(hist["Close"].iloc[-1])
    move = current_price / entry.price_at_creation - 1
    if abs(move) >= PRICE_MOVE_FLAG_THRESHOLD:
        return {"symbol": o.symbol, "move": move, "current_price": current_price}
    return None


def price_move_check() -> list[dict]:
    """For each live satellite opportunity, how far has price moved from the journal's
    price-at-creation snapshot for that symbol?"""
    entries_by_symbol = {}
    for e in get_journal_entries():
        if e.sleeve == "satellite" and e.symbol not in entries_by_symbol:
            entries_by_symbol[e.symbol] = e  # most recent first (query is desc by created_at)

    checkable = [(o, entries_by_symbol[o.symbol]) for o in get_opportunities("satellite") if o.symbol in entries_by_symbol]

    with ThreadPoolExecutor(max_workers=WATCHDOG_CONCURRENCY) as pool:
        results = list(pool.map(lambda pair: _check_price_move(*pair), checkable))
    return [r for r in results if r is not None]
