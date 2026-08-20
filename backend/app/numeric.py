"""Shared numeric-safety helper. NaN/Infinity aren't valid JSON, and Starlette's
JSONResponse (used by every route in app/main.py) raises rather than silently allowing
them through. Live-verified real bug: a single NaN produced deep in a calculator crashed
an entire API response (500, no data for anything), not just the one field that was
actually bad -- and it sailed straight past every try/except in the pipeline on the way,
since a NaN float is a normal return value, not an exception.

Two places this matters:
1. At the boundary where external data enters (e.g. app/connectors/yfinance_client.py) --
   Yahoo's own API can hand back NaN for a ratio it computed internally, before this
   codebase ever touches the number.
2. Inside any calculator working with pandas/numpy values (app/calculators/risk.py,
   technicals.py) -- unlike plain Python floats, numpy division silently produces NaN/inf
   instead of raising, so a stray gap in a price series doesn't fail loudly.

Route anything computed from real-world data through finite() before it can reach
storage or an API response.
"""

import math


def finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        if math.isnan(value) or math.isinf(value):
            return None
    except TypeError:
        return None
    return value
