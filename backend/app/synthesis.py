"""Claude synthesis layer: turns a shortlisted candidate's data bundle into a structured
research note. Only ever called on the screener's shortlist, and cached by content hash,
so re-running a scan with unchanged inputs doesn't burn another API call.
"""

import json
import logging

from anthropic import Anthropic

from app import journal, sizing
from app.screener import content_hash
from app.settings import settings
from app.store import find_cached, to_api_dict, upsert_opportunity

logger = logging.getLogger("market_copilot.synthesis")

_client: Anthropic | None = None

REQUIRED_FIELDS = (
    "thesis",
    "catalysts",
    "risks",
    "confidence",
    "invalidation_condition",
    "sizing_note",
)
MAX_ATTEMPTS = 3


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


ITEM_SEPARATOR = " | "

ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": (
        "Submit a structured research note for one candidate opportunity. Call this "
        "exactly once, filling in all six arguments as separate, plain values."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thesis": {
                "type": "string",
                "description": "2-4 sentence thesis, grounded only in the provided data bundle.",
            },
            "catalysts": {
                "type": "string",
                "description": (
                    'Specific events/conditions that would play this thesis out, as a single '
                    f'string with items separated by "{ITEM_SEPARATOR}". Example: '
                    f'"First catalyst here{ITEM_SEPARATOR}Second catalyst here"'
                ),
            },
            "risks": {
                "type": "string",
                "description": (
                    'Specific, concrete ways this thesis could be wrong, as a single string '
                    f'with items separated by "{ITEM_SEPARATOR}". Example: '
                    f'"First risk here{ITEM_SEPARATOR}Second risk here"'
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Calibrated 0-1 confidence. This is decision support, not a directive.",
            },
            "invalidation_condition": {
                "type": "string",
                "description": "The single clearest condition that would invalidate this thesis.",
            },
            "sizing_note": {
                "type": "string",
                "description": (
                    "A sizing framework note reflecting directional-conviction risk — smaller than "
                    "a market-neutral position would warrant. Never a specific dollar amount or a "
                    "'buy X' instruction."
                ),
            },
        },
        "required": [
            "thesis",
            "catalysts",
            "risks",
            "confidence",
            "invalidation_condition",
            "sizing_note",
        ],
    },
}

SYSTEM_PROMPT = """You are a rigorous buy-side research analyst producing a personal research \
note for a sophisticated individual investor who executes his own trades manually via his own \
brokerage accounts. You are not placing trades and this is not investment advice from a licensed \
advisor — it is decision support he will read and judge for himself.

Ground every claim only in the data bundle you are given. Never invent a number, headline, \
metric, or fact that is not present in the bundle. If the data is thin, say so and lower your \
confidence rather than filling gaps with assumptions.

Never issue a directive like "buy now" or "sell now" — lay out the thesis, catalysts, risks, \
and a calibrated confidence level so the reader can decide.

The opportunity is a directional conviction pick in a single equity. Be honest about \
uncertainty — confidence should rarely exceed 0.75 for a directional equity call given how little \
data a single snapshot actually contains. The bundle's metrics.insider field, when present, is a \
real signal from SEC Form 4 filings, filtered to only genuine open-market buy/sell transactions \
(compensation mechanics like option exercises and grants are already excluded, not something you \
need to second-guess). cluster_buy_signal=true means multiple distinct insiders bought on the open \
market with buying outweighing selling — treat this as a meaningfully positive, differentiated data \
point worth naming explicitly in the thesis. Heavy insider selling (negative net_value_usd) at a \
mega-cap is usually routine diversification, not a bearish signal on its own — don't overweight it \
absent other context, but do mention it factually if it's the standout number in the bundle.

Respond with exactly one call to submit_analysis, filling in each of its six arguments as \
its own separate, plain value."""


def synthesize_equity_opportunity(
    candidate: dict, macro_snapshot: dict, headlines: list[dict]
) -> dict | None:
    bundle = {
        "sleeve": "satellite",
        "type": "equity_directional",
        "ticker": candidate["ticker"],
        "name": candidate["name"],
        "metrics": candidate["metrics"],
        "macro_backdrop": macro_snapshot,
        "recent_headlines": [h["title"] for h in headlines],
    }
    return _synthesize(
        sleeve="satellite",
        symbol=candidate["ticker"],
        display_name=candidate["name"],
        bundle=bundle,
        price=candidate["metrics"].get("price"),
    )


def _synthesize(
    *,
    sleeve: str,
    symbol: str,
    display_name: str,
    bundle: dict,
    price: float | None,
) -> dict | None:
    bundle_hash = content_hash(bundle)

    cached = find_cached(symbol, sleeve, bundle_hash)
    if cached:
        return to_api_dict(cached)

    if not settings.anthropic_api_key:
        return None

    analysis = _call_claude_with_retries(symbol, bundle)
    if analysis is None:
        return None

    # Computed after the Claude call (sizing needs the confidence Claude just returned)
    # and merged in after bundle_hash was already computed above, so it never affects
    # caching -- purely additive display/report data.
    confidence = float(analysis["confidence"])
    size_info = sizing.suggested_size_satellite(confidence=confidence)

    if size_info:
        bundle["suggested_position_pct"] = size_info["suggested_position_pct"]
        bundle["sizing_method"] = size_info["method"]
        bundle["sizing_rationale"] = size_info["rationale"]

    opportunity = upsert_opportunity(
        sleeve=sleeve,
        symbol=symbol,
        display_name=display_name,
        content_hash=bundle_hash,
        thesis=analysis["thesis"],
        catalysts=_split_items(analysis["catalysts"]),
        risks=_split_items(analysis["risks"]),
        confidence=confidence,
        invalidation_condition=analysis["invalidation_condition"],
        sizing_note=analysis["sizing_note"],
        raw_metrics=bundle,
    )

    # Only on a fresh Claude call, not a cache hit above -- otherwise every unchanged
    # candidate would re-snapshot a new journal entry on every single refresh.
    journal.record_entry(
        sleeve=sleeve,
        symbol=symbol,
        thesis=analysis["thesis"],
        confidence=confidence,
        price=price,
    )

    return to_api_dict(opportunity)


def _call_claude_with_retries(symbol: str, bundle: dict) -> dict | None:
    """Calls Claude for a structured analysis, retrying with a fresh independent call if
    the response doesn't actually match the schema (forced tool_choice normally
    guarantees this, but models occasionally still collapse everything into one field).
    A fresh call — rather than continuing the malformed conversation — sidesteps having
    to fabricate a matching tool_result for the bad tool_use block.
    """
    user_message = {
        "role": "user",
        "content": f"Data bundle:\n{json.dumps(bundle, indent=2, default=str)}",
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = _get_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=1536,
            system=SYSTEM_PROMPT,
            tools=[ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "submit_analysis"},
            messages=[user_message],
        )

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use and _is_valid_analysis(tool_use.input):
            return tool_use.input

        logger.warning(
            "Synthesis attempt %d/%d for %s returned a malformed schema, %s",
            attempt,
            MAX_ATTEMPTS,
            symbol,
            "retrying" if attempt < MAX_ATTEMPTS else "giving up",
        )

    return None


def _is_valid_analysis(analysis: dict) -> bool:
    if not isinstance(analysis, dict):
        return False
    if any(field not in analysis for field in REQUIRED_FIELDS):
        return False
    if not isinstance(analysis["catalysts"], str) or not isinstance(analysis["risks"], str):
        return False
    try:
        confidence = float(analysis["confidence"])
    except (TypeError, ValueError):
        return False
    return 0 <= confidence <= 1


def _split_items(text: str) -> list[str]:
    return [item.strip() for item in text.split(ITEM_SEPARATOR) if item.strip()]
