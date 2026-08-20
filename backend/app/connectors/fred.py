"""FRED macro series. Free but requires a personal API key from https://fred.stlouisfed.org/docs/api/api_key.html"""

import httpx

from app.settings import settings

BASE = "https://api.stlouisfed.org/fred/series/observations"

# Series we use as macro backdrop context for equity synthesis. CPIAUCSL is the raw
# seasonally-adjusted index level FRED returns, not a year-over-year % change -- despite
# an earlier version of this dict calling it "cpi_yoy", a single `limit=1` observation
# has no second point to compute a YoY change from. Named for what it actually is; a real
# YoY figure would need a second observation ~12 months back and a % change calc, not done
# here yet.
MACRO_SERIES = {
    "fed_funds_rate": "DFF",
    "cpi_index": "CPIAUCSL",
    "ten_year_yield": "DGS10",
    "unemployment_rate": "UNRATE",
}


def get_latest_macro_snapshot() -> dict:
    """Returns the latest observed value for each tracked macro series. Skips series it can't fetch."""
    if not settings.fred_api_key:
        return {}

    snapshot = {}
    try:
        with httpx.Client(timeout=10) as client:
            for label, series_id in MACRO_SERIES.items():
                resp = client.get(
                    BASE,
                    params={
                        "series_id": series_id,
                        "api_key": settings.fred_api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": 1,
                    },
                )
                resp.raise_for_status()
                observations = resp.json().get("observations", [])
                if observations and observations[0]["value"] != ".":
                    snapshot[label] = {
                        "value": float(observations[0]["value"]),
                        "date": observations[0]["date"],
                    }
    except (httpx.HTTPError, KeyError, ValueError):
        pass

    return snapshot
