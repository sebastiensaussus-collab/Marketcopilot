"""Outcome journal: checks every freshly-synthesized thesis against what actually
happened at fixed horizons, so confidence and signal quality can eventually be judged
from evidence instead of taken on faith.

Outcomes measure price return and excess return vs SPY, since these theses are
directional. See app/store.py's JournalEntry for the schema.

Nothing here is useful until entries actually age past a horizon -- a fresh install has
an empty journal, and the first real read comes 14 days after the first synthesis run.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.connectors import yfinance_client
from app.store import (
    create_journal_entry,
    get_journal_entries,
    get_journal_entries_due,
    save_journal_entry,
)

logger = logging.getLogger("market_copilot.journal")

HORIZONS_DAYS = (14, 30, 90)


def record_entry(*, sleeve: str, symbol: str, thesis: str, confidence: float, price: float | None) -> None:
    if price is None:
        logger.warning("No price available to snapshot journal entry for %s -- skipping", symbol)
        return
    create_journal_entry(sleeve=sleeve, symbol=symbol, thesis=thesis, confidence=confidence, price=price)


def _last_price_at_or_before(hist, cutoff: datetime) -> float | None:
    window = hist[hist.index <= cutoff]
    if window.empty:
        return None
    return float(window["Close"].iloc[-1])


def _review_equity(symbol: str, price_at_creation: float, start: datetime, end: datetime) -> tuple[float | None, float | None]:
    """(price_return, excess_return_vs_spy) over [start, end]."""
    hist = yfinance_client.get_price_history_range(symbol, start - timedelta(days=5), end)
    spy_hist = yfinance_client.get_price_history_range("SPY", start - timedelta(days=5), end)
    if hist is None or spy_hist is None:
        return None, None

    end_price = _last_price_at_or_before(hist, end)
    if end_price is None:
        return None, None
    price_return = end_price / price_at_creation - 1

    spy_start_price = _last_price_at_or_before(spy_hist, start)
    spy_end_price = _last_price_at_or_before(spy_hist, end)
    if spy_start_price is None or spy_end_price is None:
        return price_return, None

    spy_return = spy_end_price / spy_start_price - 1
    return price_return, price_return - spy_return


def review_due_entries() -> dict:
    """Fills in outcome fields for any entry that's aged past a horizon and hasn't been
    checked yet. Safe to call often -- entries not yet due are simply skipped."""
    reviewed = {14: 0, 30: 0, 90: 0}

    for horizon in HORIZONS_DAYS:
        return_field = f"return_{horizon}d"
        checked_field = f"checked_{horizon}d_at"
        excess_field = f"excess_return_{horizon}d"

        for entry in get_journal_entries_due(horizon, return_field):
            if entry.sleeve == "core":
                continue  # legacy crypto entries from before the sleeve was removed -- no reviewer for these anymore

            start = entry.created_at.replace(tzinfo=timezone.utc)
            end = start + timedelta(days=horizon)

            price_return, excess_return = _review_equity(entry.symbol, entry.price_at_creation, start, end)
            setattr(entry, return_field, price_return)
            setattr(entry, excess_field, excess_return)

            setattr(entry, checked_field, datetime.now(timezone.utc))
            save_journal_entry(entry)
            reviewed[horizon] += 1

    return reviewed


def calibration_summary(entries: list | None = None) -> dict:
    """Buckets matured entries by confidence and compares average confidence to actual
    hit rate in each bucket -- the whole point of the journal. Only counts entries that
    have at least one matured horizon. `entries` is injectable for testing without a DB.
    """
    if entries is None:
        entries = get_journal_entries()
    buckets: dict[str, list] = {}

    for entry in entries:
        for horizon in HORIZONS_DAYS:
            outcome = getattr(entry, f"return_{horizon}d")
            if outcome is None:
                continue
            hit = outcome > 0
            bucket_key = f"{int(entry.confidence_at_creation * 10) * 10}-{int(entry.confidence_at_creation * 10) * 10 + 10}%"
            buckets.setdefault(bucket_key, []).append((entry.confidence_at_creation, hit))

    summary = {}
    for bucket, records in buckets.items():
        confidences = [c for c, _ in records]
        hits = [h for _, h in records]
        summary[bucket] = {
            "num_entries": len(records),
            "avg_confidence": sum(confidences) / len(confidences),
            "actual_hit_rate": sum(hits) / len(hits),
        }

    return {
        "total_entries": len(entries),
        "matured_entries": sum(len(r) for r in buckets.values()),
        "calibration_buckets": summary,
    }
