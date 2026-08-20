"""Unifies IBKR positions with manually-imported ING/Bolero holdings into one view.
Read-only end to end -- nothing here can place an order (see app/connectors/ibkr.py).
"""

import base64
import csv
import io
import logging
from typing import Optional

from anthropic import Anthropic

from app.connectors import ibkr
from app.settings import settings
from app.store import get_manual_holdings, replace_manual_holdings, upsert_manual_trade

logger = logging.getLogger("market_copilot.portfolio")

REQUIRED_CSV_COLUMNS = {"symbol", "quantity"}


def parse_holdings_csv(content: str) -> list[dict]:
    """Expects a header row with at least `symbol` and `quantity` columns, plus an
    optional `average_cost`. Column order doesn't matter. Skips blank rows. Raises
    ValueError with a clear message on a malformed file rather than silently guessing.
    """
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValueError("CSV appears to be empty")

    headers = {h.strip().lower() for h in reader.fieldnames}
    missing = REQUIRED_CSV_COLUMNS - headers
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    holdings = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        normalized = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
        symbol = normalized.get("symbol")
        quantity_raw = normalized.get("quantity")
        if not symbol and not quantity_raw:
            continue  # blank line
        if not symbol or not quantity_raw:
            raise ValueError(f"Row {i}: symbol and quantity are both required")

        try:
            quantity = float(quantity_raw)
        except ValueError:
            raise ValueError(f"Row {i}: quantity '{quantity_raw}' is not a number") from None

        average_cost = None
        avg_cost_raw = normalized.get("average_cost")
        if avg_cost_raw:
            try:
                average_cost = float(avg_cost_raw)
            except ValueError:
                raise ValueError(f"Row {i}: average_cost '{avg_cost_raw}' is not a number") from None

        holdings.append({"symbol": symbol.upper(), "quantity": quantity, "average_cost": average_cost})

    return holdings


def import_csv(broker: str, content: str) -> int:
    holdings = parse_holdings_csv(content)
    replace_manual_holdings(broker, holdings)
    return len(holdings)


def confirm_import(broker: str, holdings: list[dict]) -> int:
    """Commits a (possibly user-edited) set of holdings from the document-import review
    step -- same full-replace-per-broker path as import_csv, just fed from a reviewed
    extraction instead of a hand-built CSV."""
    replace_manual_holdings(broker, holdings)
    return len(holdings)


def get_unified_portfolio() -> dict:
    ibkr_positions = ibkr.fetch_positions()
    account_summary = ibkr.fetch_account_summary() if ibkr_positions is not None else None

    manual = {}
    for holding in get_manual_holdings():
        manual.setdefault(holding.broker, []).append(
            {"symbol": holding.symbol, "quantity": holding.quantity, "average_cost": holding.average_cost}
        )

    combined: dict[str, float] = {}
    for p in ibkr_positions or []:
        combined[p["symbol"]] = combined.get(p["symbol"], 0) + p["position"]
    for rows in manual.values():
        for h in rows:
            combined[h["symbol"]] = combined.get(h["symbol"], 0) + h["quantity"]

    return {
        "ibkr": {
            "connected": ibkr_positions is not None,
            "positions": ibkr_positions or [],
            "account_summary": account_summary or {},
        },
        "manual": manual,
        "combined_quantity_by_symbol": combined,
    }


def compute_trade_result(
    old_quantity: float, old_average_cost: Optional[float], action: str, quantity: float, price: float
) -> tuple[float, Optional[float]]:
    """Pure arithmetic for a single buy/sell against an existing (possibly zero) position.
    Buying weighted-averages into the existing cost basis; selling leaves cost basis
    unchanged (selling doesn't change the remaining shares' cost) and raises if it would
    sell more than is actually held -- a loud failure instead of a fabricated negative
    position. Returns (new_quantity, new_average_cost); new_quantity <= 0 means closed.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    if action == "buy":
        new_quantity = old_quantity + quantity
        if old_quantity <= 0 or old_average_cost is None:
            new_average_cost = price
        else:
            new_average_cost = (old_quantity * old_average_cost + quantity * price) / new_quantity
        return new_quantity, new_average_cost

    if action == "sell":
        if quantity > old_quantity:
            raise ValueError(f"Cannot sell {quantity} -- only {old_quantity} held")
        return old_quantity - quantity, old_average_cost

    raise ValueError(f"Unknown action '{action}' -- expected 'buy' or 'sell'")


def record_trade(broker: str, symbol: str, action: str, quantity: float, price: float) -> Optional[dict]:
    """Applies a single buy/sell to `broker`'s holding in `symbol`, creating the row on a
    first buy or deleting it on a sale that closes the position -- unlike
    replace_manual_holdings this only ever touches the one (broker, symbol) row.
    """
    symbol = symbol.upper()
    existing = next((h for h in get_manual_holdings(broker) if h.symbol == symbol), None)
    old_quantity = existing.quantity if existing else 0.0
    old_average_cost = existing.average_cost if existing else None

    new_quantity, new_average_cost = compute_trade_result(old_quantity, old_average_cost, action, quantity, price)
    row = upsert_manual_trade(broker, symbol, new_quantity, new_average_cost)
    if row is None:
        return None
    return {"broker": row.broker, "symbol": row.symbol, "quantity": row.quantity, "average_cost": row.average_cost}


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


EXTRACTION_MAX_ATTEMPTS = 3

EXTRACT_HOLDINGS_TOOL = {
    "name": "submit_holdings",
    "description": (
        "Submit every security position found in this broker statement, exactly once. "
        "Transcribe only what is literally printed -- never estimate, infer, or fabricate "
        "a number that isn't shown on the page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "holdings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Full security name exactly as printed."},
                        "isin": {"type": "string", "description": "ISIN, if printed. Omit entirely if not shown."},
                        "symbol_guess": {
                            "type": "string",
                            "description": (
                                "Best-effort guess at the trading ticker, inferred from the name/ISIN/"
                                "exchange. A guess to be confirmed by the user, not a fact read from the page."
                            ),
                        },
                        "quantity": {"type": "number", "description": "Units/shares held, as printed."},
                        "average_cost": {
                            "type": "number",
                            "description": "Average purchase price per unit, if printed. Omit entirely if not shown.",
                        },
                        "currency": {
                            "type": "string",
                            "description": "ISO currency code the position is quoted in, if shown. Omit if not shown.",
                        },
                    },
                    "required": ["name", "symbol_guess", "quantity"],
                },
            },
        },
        "required": ["holdings"],
    },
}

EXTRACTION_SYSTEM_PROMPT = """You read broker portfolio statements (PDF or screenshot) and transcribe the \
security positions they contain into structured data. You are reading a real financial document for someone \
who will review your output before anything is saved -- accuracy matters more than completeness.

Rules:
- Only report a holding if it is an actual security position (shares, ETF units, bonds) with a quantity. \
Skip cash balances, summary/total rows, and anything that isn't a specific position.
- Never estimate, infer, or round a number that isn't printed on the page. If average cost or currency \
isn't shown, omit that field entirely rather than guessing a value.
- symbol_guess is explicitly allowed to be a guess (inferred from the name, ISIN, and exchange) since \
statements rarely print the trading ticker directly -- but say so only there; every other field must be a \
literal transcription."""


def _is_valid_extraction(holdings) -> bool:
    if not isinstance(holdings, list):
        return False
    for h in holdings:
        if not isinstance(h, dict):
            return False
        if not isinstance(h.get("name"), str) or not h["name"]:
            return False
        if not isinstance(h.get("symbol_guess"), str) or not h["symbol_guess"]:
            return False
        if not isinstance(h.get("quantity"), (int, float)):
            return False
    return True


def _call_claude_extraction_with_retries(file_bytes: bytes, media_type: str) -> list[dict]:
    """Mirrors app/synthesis.py's forced-tool-use + retry-with-a-fresh-call pattern: a
    fresh call (not a continued conversation) sidesteps having to fabricate a matching
    tool_result for a malformed response."""
    block_type = "document" if media_type == "application/pdf" else "image"
    user_message = {
        "role": "user",
        "content": [
            {
                "type": block_type,
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(file_bytes).decode("ascii")},
            },
            {"type": "text", "text": "Extract every security position from this broker statement."},
        ],
    }

    for attempt in range(1, EXTRACTION_MAX_ATTEMPTS + 1):
        response = _get_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=EXTRACTION_SYSTEM_PROMPT,
            tools=[EXTRACT_HOLDINGS_TOOL],
            tool_choice={"type": "tool", "name": "submit_holdings"},
            messages=[user_message],
        )

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        holdings = tool_use.input.get("holdings") if tool_use else None
        if holdings is not None and _is_valid_extraction(holdings):
            return holdings

        logger.warning(
            "Document extraction attempt %d/%d returned a malformed schema, %s",
            attempt,
            EXTRACTION_MAX_ATTEMPTS,
            "retrying" if attempt < EXTRACTION_MAX_ATTEMPTS else "giving up",
        )

    raise ValueError("Could not read holdings from this document -- try a clearer copy or enter them manually.")


def extract_holdings_from_document(file_bytes: bytes, media_type: str) -> list[dict]:
    """Sends a broker statement (PDF or screenshot) to Claude and returns the holdings it
    read off the page: `name`, `isin`, `symbol_guess`, `quantity`, `average_cost`,
    `currency` (fields absent from the document are omitted, not guessed). Purely a read
    -- never touches the DB. The caller must run this through a review step before
    persisting anything (see /portfolio/confirm-import), since symbol_guess in particular
    is explicitly a guess, not a fact.
    """
    raw_holdings = _call_claude_extraction_with_retries(file_bytes, media_type)
    return [
        {
            "name": h["name"],
            "isin": h.get("isin"),
            "symbol_guess": h["symbol_guess"].upper(),
            "quantity": h["quantity"],
            "average_cost": h.get("average_cost"),
            "currency": h.get("currency"),
        }
        for h in raw_holdings
    ]
