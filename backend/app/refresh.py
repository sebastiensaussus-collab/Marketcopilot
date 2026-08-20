"""The core "run a full scan + synthesis pass" orchestration, factored out of main.py so
both the /refresh endpoint and the morning report can call the exact same logic without
main.py and app/reports.py importing each other.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from app import journal, screener, synthesis
from app.connectors import fred, news_rss
from app.settings import settings

logger = logging.getLogger("market_copilot.refresh")


def _synthesize_equity_safe(candidate: dict, macro_snapshot: dict) -> dict | None:
    headlines = news_rss.get_recent_headlines(f"{candidate['name']} stock")
    try:
        return synthesis.synthesize_equity_opportunity(candidate, macro_snapshot, headlines)
    except Exception:
        logger.exception("Equity synthesis errored for %s", candidate["ticker"])
        return None


def run_full_refresh() -> dict:
    """Runs the screener, synthesizes the shortlist concurrently, and returns run
    stats. This is the only path that spends Claude API calls -- the lunch/evening
    reports deliberately avoid calling this again, see app/reports.py.
    """
    journal.review_due_entries()

    equity_candidates = screener.scan_equities()

    macro_snapshot = fred.get_latest_macro_snapshot()

    with ThreadPoolExecutor(max_workers=settings.synthesis_concurrency) as pool:
        equity_futures = [
            pool.submit(_synthesize_equity_safe, c, macro_snapshot) for c in equity_candidates
        ]
        equity_results = [f.result() for f in equity_futures]

    for candidate, result in zip(equity_candidates, equity_results):
        if not result:
            logger.warning(
                "Skipped equity synthesis for %s (no API key or malformed response)",
                candidate["ticker"],
            )

    return {
        "equity_candidates_scanned": len(equity_candidates),
        "satellite_opportunities_synthesized": sum(1 for r in equity_results if r),
    }
