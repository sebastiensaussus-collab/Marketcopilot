"""Cross-references synthesized opportunities against what Sebastien actually holds to
produce the full buy/hold/sell picture for today -- not a directive, still research
context, but netted against his real portfolio and estimated fees/taxes, with suggested
reference price levels (limit/stop/target) instead of a bare ranked shortlist.

Zero new model API cost: pure arithmetic over opportunities/holdings/macro data everything
else in this app already fetches (app/sizing.py's suggested_position_pct, app/portfolio_risk.py's
priced holdings, app/connectors/fred.py's macro snapshot, app/watchdog.py's invalidation checks).

Every price level here is a suggested reference, not an order this app places or a
prediction to trust blindly -- see the per-field notes below for what each is actually
grounded in.
"""

import json
from datetime import datetime, timezone

from app import watchdog
from app.calculators import fees
from app.connectors import fred
from app.portfolio import available_cash_eur
from app.portfolio_risk import (
    CONCENTRATION_THRESHOLD,
    HIGH_CORRELATION_THRESHOLD,
    _names_by_symbol,
    correlation_analysis,
    priced_holdings,
)
from app.store import get_opportunities

ACTION_TOLERANCE_PCT = 0.3  # relative gap between held and suggested weight before flagging
MIN_ACTIONABLE_EUR = 50  # skip trades this small -- fee/tax drag would dominate

# Only the largest few holdings are checked against a new buy candidate -- bounds the
# extra yfinance history calls, and in a portfolio this concentrated (verified live: the
# top 2 holdings alone are ~70%+ of NAV) the largest few already capture the overwhelming
# majority of practically relevant compounding risk.
CROSS_SLEEVE_TOP_HOLDINGS_TO_CHECK = 5

# A small, explicitly-labeled convention for where to place a limit order relative to the
# last scanned price -- not a data-driven prediction of where the price will go.
LIMIT_BUFFER_PCT = 0.005

BUCKET_BY_ACTION = {
    "new_entry": "buy",
    "add": "buy",
    "trim": "sell",
    "exit": "sell",
    "trim_unmanaged": "sell",
    "hold": "hold",
    "hold_unmanaged": "hold",
}

# The two conditions that also drive app/alerts.py's urgent email checks (price-move
# invalidation, concentration breach) -- centralized here so the frontend doesn't have to
# reverse-engineer "which action strings count as urgent" itself, the same reasoning as
# BUCKET_BY_ACTION above. A regular "trim" (overweight vs. model-suggested size) or a new
# buy idea is portfolio management, not urgent -- only these two are.
URGENT_ACTIONS = {"exit", "trim_unmanaged"}

# Holdings outside the model's SP100 scan universe (most European ETFs, e.g.) never get
# a per-symbol thesis, so they can never trigger a normal trim/exit -- but a position can
# still be flagged purely on diversification math the app already computes (see
# app/portfolio_risk.py's concentration analysis), without fabricating a directional view
# on an instrument the model has no way to actually analyze.
CONCENTRATION_IS_US_LISTED = False  # holdings outside the SP100 universe are, in
# practice, the European-listed ETFs imported via ING/Bolero -- not a hard guarantee, just
# the best available default given no per-holding listing-venue data exists yet.

# The satellite universe (app/universe.py's SP100) is individual US-listed equities, not
# ETFs -- TOB is estimated at the flat equity rate rather than guessing per-symbol ETF
# domicile/distribution policy (see app/calculators/fees.py for the ETF rates, unused here).
SATELLITE_INSTRUMENT_TYPE = "equity"
SATELLITE_IS_US_LISTED = True


def _held_value_by_symbol(priced: list[dict]) -> dict[str, float]:
    by_symbol: dict[str, float] = {}
    for h in priced:
        by_symbol[h["symbol"]] = by_symbol.get(h["symbol"], 0.0) + h["value_eur"]
    return by_symbol


def _classify(held_eur: float, suggested_eur: float | None, invalidated: bool) -> str | None:
    """None means "nothing worth saying" (not held, and no meaningful suggested size) --
    excluded entirely rather than shown as a no-op. "hold" means held and in-tolerance --
    shown, not dropped, since Sebastien wants the full picture, not just deltas."""
    if invalidated and held_eur > 0:
        return "exit"
    if suggested_eur is None:
        return None
    delta = suggested_eur - held_eur
    if abs(delta) < MIN_ACTIONABLE_EUR:
        return "hold" if held_eur > 0 else None
    baseline = max(held_eur, suggested_eur)
    if baseline > 0 and abs(delta) / baseline < ACTION_TOLERANCE_PCT:
        return "hold" if held_eur > 0 else None
    if held_eur <= 0:
        return "new_entry"
    return "add" if delta > 0 else "trim"


def _instrument_type_from_name(name: str) -> str:
    """Belgian TOB rate depends on instrument type (see app/calculators/fees.py), which
    isn't tracked per-holding -- inferred from the resolved fund name text itself (e.g.
    "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)") rather than guessed
    blind. Defaults to the higher accumulating-ETF rate when a fund's name doesn't clearly
    say (Dist) -- errs toward not understating the tax, not toward the lower number.
    """
    upper = name.upper()
    if "ETF" not in upper and "UCITS" not in upper:
        return "equity"
    if "(DIST)" in upper or "DISTRIBUTING" in upper:
        return "etf_distributing"
    return "etf_accumulating"


# Broad asset class, inferred from the resolved fund/company name the same way
# _instrument_type_from_name is -- used for the portfolio-wide allocation breakdown and
# to decide what (if any) factual macro context is relevant to a given uncovered holding.
# Keyword-based and coarse by design: good enough to tell "this is a bond fund" from "this
# is a stock" from real name text, not a claim about a specific instrument's mechanics.
ASSET_CLASS_BOND_KEYWORDS = ("BOND", "AGGREGATE", "AGG ", " AGG", "TREASURY", "GILT", "FIXED INCOME")
ASSET_CLASS_COMMODITY_KEYWORDS = ("GOLD", "SILVER", "COMMODITY", "PRECIOUS METAL")


def _asset_class(name: str) -> str:
    upper = name.upper()
    if any(kw in upper for kw in ASSET_CLASS_COMMODITY_KEYWORDS):
        return "commodity"
    if any(kw in upper for kw in ASSET_CLASS_BOND_KEYWORDS):
        return "bond"
    return "equity"


def _macro_context_line(asset_class: str, macro_snapshot: dict) -> str | None:
    """Factual context only -- never a forecast of where any of these numbers go next.
    Only fires for holdings the model has no per-symbol thesis for (see build_today_actions
    below); covered satellite equities already get real macro context baked into their
    model-synthesized thesis via the same macro_snapshot, so adding this there too would
    just be redundant, not additive.
    """
    if not macro_snapshot:
        return None
    ten_year = macro_snapshot.get("ten_year_yield", {}).get("value")
    fed_funds = macro_snapshot.get("fed_funds_rate", {}).get("value")

    if asset_class == "bond":
        if ten_year is None:
            return None
        rate_note = f", Fed funds {fed_funds}%" if fed_funds is not None else ""
        return (
            f"Macro context: 10Y yield {ten_year}%{rate_note}. Bond fund prices move "
            f"inversely with yields (duration risk) -- a mechanical fact about how these "
            f"instruments work, not a forecast of where yields go next."
        )

    if asset_class == "equity":
        parts = [p for p in (f"Fed funds {fed_funds}%" if fed_funds is not None else None,
                              f"10Y yield {ten_year}%" if ten_year is not None else None) if p]
        unemployment = macro_snapshot.get("unemployment_rate", {}).get("value")
        if unemployment is not None:
            parts.append(f"unemployment {unemployment}%")
        if not parts:
            return None
        return f"Macro backdrop: {', '.join(parts)} -- shown for context, not a directional call."

    return None


def _asset_allocation(priced: list[dict]) -> dict:
    """Real portfolio composition by broad asset class -- the per-symbol concentration
    view (app/portfolio_risk.py) already shows weight by ticker, but doesn't answer "am I
    actually diversified across asset classes" without manually adding tickers up
    yourself. Same real value_eur data, just grouped differently."""
    totals: dict[str, float] = {}
    for h in priced:
        asset_class = _asset_class(h.get("name") or h["symbol"])
        totals[asset_class] = totals.get(asset_class, 0.0) + h["value_eur"]

    total = sum(totals.values())
    if total <= 0:
        return {"total_value_eur": 0.0, "weights": {}}
    return {"total_value_eur": round(total, 2), "weights": {k: round(v / total, 4) for k, v in totals.items()}}


def _correlated_holdings_for_candidates(
    candidate_symbols: list[str], priced: list[dict], nav_eur: float
) -> dict[str, list[dict]]:
    """For each satellite buy candidate, which of the portfolio's largest holdings is it
    highly correlated with -- a real cross-sleeve check: a new equity pick can look
    attractive on its own thesis while actually just compounding exposure the portfolio
    already has via an ETF (e.g. a mega-cap US stock and a global equity index tracker
    that's heavily weighted toward the same names), not diversifying it. Bounded to the
    largest few holdings by weight (see CROSS_SLEEVE_TOP_HOLDINGS_TO_CHECK) to keep the
    extra network calls reasonable rather than checking every single holding.
    """
    if not candidate_symbols or nav_eur <= 0:
        return {}

    weight_by_symbol: dict[str, float] = {}
    for h in priced:
        weight_by_symbol[h["symbol"]] = weight_by_symbol.get(h["symbol"], 0.0) + h["value_eur"] / nav_eur

    top_holdings = dict(
        sorted(weight_by_symbol.items(), key=lambda kv: kv[1], reverse=True)[:CROSS_SLEEVE_TOP_HOLDINGS_TO_CHECK]
    )
    held_symbols = [sym for sym in top_holdings if sym not in candidate_symbols]
    if not held_symbols:
        return {}

    result = correlation_analysis(sorted(set(held_symbols) | set(candidate_symbols)))

    correlated_by_candidate: dict[str, list[dict]] = {}
    for pair in result["pairs"]:
        a, b, corr = pair["symbol_a"], pair["symbol_b"], pair["correlation"]
        if corr < HIGH_CORRELATION_THRESHOLD:
            continue
        for candidate, held in ((a, b), (b, a)):
            if candidate in candidate_symbols and held in top_holdings:
                correlated_by_candidate.setdefault(candidate, []).append(
                    {"symbol": held, "correlation": corr, "weight": round(top_holdings[held], 4)}
                )

    return correlated_by_candidate


def _fee_for(trade_value_eur: float) -> dict:
    broker_est = fees.cheapest_broker_estimate(trade_value_eur, SATELLITE_IS_US_LISTED)
    tob_eur = fees.estimate_tob(trade_value_eur, SATELLITE_INSTRUMENT_TYPE)
    return {"broker": broker_est["broker"], "fee_eur": broker_est["fee_eur"], "tob_eur": round(tob_eur, 2)}


def _rationale(action: str, held_eur: float, suggested_eur: float | None, fee_total: float) -> str:
    if action == "exit":
        return "Currently held, but the thesis looks invalidated (price moved past threshold) -- worth reviewing whether to close."
    if action == "new_entry":
        return f"Not currently held. Model-suggested weight implies an estimated €{suggested_eur:.0f} position, after ~€{fee_total:.2f} in estimated fees/taxes."
    if action == "add":
        gap = suggested_eur - held_eur
        return f"Held below model-suggested weight by an estimated €{gap:.0f}, after ~€{fee_total:.2f} in estimated fees/taxes."
    if action == "trim":
        gap = held_eur - suggested_eur
        return f"Held above model-suggested weight by an estimated €{gap:.0f}."
    if action == "hold":
        return f"Held at roughly the model-suggested weight (€{held_eur:.0f} vs €{suggested_eur:.0f} suggested) -- no change indicated."
    return ""


def _reference_price(raw_metrics: dict) -> float | None:
    return raw_metrics.get("metrics", {}).get("price")


def _dcf_target(raw_metrics: dict) -> float | None:
    return raw_metrics.get("metrics", {}).get("dcf_fair_value")


def _price_levels(*, action: str, reference_price: float | None, dcf_fair_value: float | None) -> dict:
    """Every level here is a suggested reference, not a prediction:
    - limit_price: last scanned price +/- a small fixed buffer (LIMIT_BUFFER_PCT) --
      below for buys (avoid chasing), at-market for an urgent exit, a touch above for a
      non-urgent trim. A convention, not a forecast.
    - stop_price: reference_price * (1 - watchdog.PRICE_MOVE_FLAG_THRESHOLD) -- the *same*
      threshold that already triggers this app's own invalidation watchdog, so the stop
      and the "is this thesis still valid" check agree with each other.
    - stop_condition: always None now (was a core/crypto-only funding-flip condition;
      kept as a field for API shape stability with the two uncovered-holding action
      dicts below, which also always set it to None).
    - target_price (when available): the DCF fair value from app/calculators/dcf.py -- a
      rough sanity check per that module's own docstring, not a price target to trust
      blindly.
    """
    levels = {
        "reference_price": round(reference_price, 4) if reference_price is not None else None,
        "limit_price": None,
        "stop_price": None,
        "stop_condition": None,
        "target_price": None,
    }
    if reference_price is None:
        return levels

    if action in ("new_entry", "add"):
        levels["limit_price"] = round(reference_price * (1 - LIMIT_BUFFER_PCT), 4)
    elif action == "trim":
        levels["limit_price"] = round(reference_price * (1 + LIMIT_BUFFER_PCT), 4)
    elif action == "exit":
        levels["limit_price"] = round(reference_price, 4)

    if action in ("new_entry", "add", "hold"):
        levels["stop_price"] = round(reference_price * (1 - watchdog.PRICE_MOVE_FLAG_THRESHOLD), 4)
        if dcf_fair_value is not None:
            levels["target_price"] = round(dcf_fair_value, 2)

    return levels


def build_today_actions(
    *,
    opportunities: list | None = None,
    priced: list[dict] | None = None,
    macro_snapshot: dict | None = None,
    price_moves: list[dict] | None = None,
    correlated_holdings: dict[str, list[dict]] | None = None,
    available_cash: dict | None = None,
) -> dict:
    """Injectable args are for testing without live network/DB -- mirrors app/journal.py's
    calibration_summary(entries=None) pattern. price_moves also lets a caller that already
    computed the watchdog check (see app/reports.py's pulse reports) pass it through
    instead of re-fetching live data a second time in the same report.
    correlated_holdings skips the live cross-sleeve correlation lookup entirely when
    supplied (even as {}) -- it's a real yfinance history call per candidate/holding, not
    something every test should pay for just by constructing a satellite buy action.
    available_cash (see app/portfolio.available_cash_eur) gates which buy actions are
    actually actionable -- see the reclassification pass below.
    """
    if opportunities is None:
        opportunities = get_opportunities("satellite")
    if priced is None:
        priced, unpriced_symbols = priced_holdings()
    else:
        unpriced_symbols = []
    if macro_snapshot is None:
        macro_snapshot = fred.get_latest_macro_snapshot()
    if price_moves is None:
        price_moves = watchdog.price_move_check()
    if available_cash is None:
        available_cash = available_cash_eur()

    nav_eur = sum(h["value_eur"] for h in priced)
    held_by_symbol = _held_value_by_symbol(priced)
    names_by_symbol = _names_by_symbol(priced)

    invalidated_symbols = {m["symbol"] for m in price_moves}

    actions = []
    for o in opportunities:
        raw_metrics = json.loads(o.raw_metrics_json) if o.raw_metrics_json else {}
        suggested_pct = raw_metrics.get("suggested_position_pct")
        held_eur = held_by_symbol.get(o.symbol, 0.0)
        suggested_eur = suggested_pct * nav_eur if suggested_pct is not None and nav_eur > 0 else None
        invalidated = o.symbol in invalidated_symbols

        action = _classify(held_eur, suggested_eur, invalidated)
        if action is None:
            continue

        fee_info = None
        fee_pct = None
        fee_total = 0.0
        if action != "hold":
            trade_value_eur = held_eur if action == "exit" else abs(suggested_eur - held_eur)
            fee_info = _fee_for(trade_value_eur)
            fee_total = fee_info["fee_eur"] + (fee_info["tob_eur"] or 0)
            fee_pct = fee_total / trade_value_eur if trade_value_eur > 0 else None

        reference_price = _reference_price(raw_metrics)
        dcf_fair_value = _dcf_target(raw_metrics)
        levels = _price_levels(action=action, reference_price=reference_price, dcf_fair_value=dcf_fair_value)

        actions.append(
            {
                "symbol": o.symbol,
                "sleeve": o.sleeve,
                "display_name": o.display_name,
                "action": action,
                "bucket": BUCKET_BY_ACTION[action],
                "urgent": action in URGENT_ACTIONS,
                "asset_class": _asset_class(o.display_name),
                "confidence": o.confidence,
                "suggested_pct": suggested_pct,
                "suggested_eur": round(suggested_eur, 2) if suggested_eur is not None else None,
                "held_eur": round(held_eur, 2),
                "fee": fee_info,
                "fee_pct_of_trade": round(fee_pct, 4) if fee_pct is not None else None,
                "fee_efficient": (fee_pct < 0.01) if fee_pct is not None else None,
                "rationale": _rationale(action, held_eur, suggested_eur, fee_total),
                "thesis": o.thesis,
                "correlated_holdings": [],
                "cash_available": None,
                **levels,
            }
        )

    # Cross-holding correlation check: a new buy can look attractive on its own thesis
    # while actually just compounding exposure already held via an ETF, not diversifying
    # it -- the model didn't check that before.
    buy_symbols = [a["symbol"] for a in actions if a["bucket"] == "buy"]
    if correlated_holdings is None:
        correlated_by_candidate = _correlated_holdings_for_candidates(buy_symbols, priced, nav_eur)
    else:
        correlated_by_candidate = correlated_holdings
    for a in actions:
        matches = correlated_by_candidate.get(a["symbol"])
        if not matches:
            continue
        a["correlated_holdings"] = matches
        parts = [f"{m['symbol']} ({round(m['weight'] * 100)}% of portfolio, correlation {m['correlation']:.2f})" for m in matches]
        a["rationale"] += (
            f" Correlation check: highly correlated with {', '.join(parts)} -- would compound "
            f"existing exposure, not diversify it."
        )

    covered_symbols = {o.symbol for o in opportunities}
    for symbol, held_eur in held_by_symbol.items():
        if symbol in covered_symbols or held_eur <= 0:
            continue

        name = names_by_symbol.get(symbol, symbol)
        weight = held_eur / nav_eur if nav_eur > 0 else 0.0
        asset_class = _asset_class(name)
        macro_line = _macro_context_line(asset_class, macro_snapshot)

        if weight > CONCENTRATION_THRESHOLD:
            target_eur = CONCENTRATION_THRESHOLD * nav_eur
            trim_eur = held_eur - target_eur
            if trim_eur >= MIN_ACTIONABLE_EUR:
                instrument_type = _instrument_type_from_name(name)
                broker_est = fees.cheapest_broker_estimate(trim_eur, CONCENTRATION_IS_US_LISTED)
                tob_eur = round(fees.estimate_tob(trim_eur, instrument_type), 2)
                fee_info = {"broker": broker_est["broker"], "fee_eur": broker_est["fee_eur"], "tob_eur": tob_eur}
                fee_pct = (fee_info["fee_eur"] + tob_eur) / trim_eur
                rationale = (
                    f"{round(weight * 100)}% of your priced portfolio -- concentrated beyond the "
                    f"{round(CONCENTRATION_THRESHOLD * 100)}% diversification threshold. No per-symbol "
                    f"thesis exists for this holding (outside the model's equity scan universe), "
                    f"but this is flagged purely on diversification grounds, not a directional view."
                )
                if macro_line:
                    rationale += " " + macro_line
                actions.append(
                    {
                        "symbol": symbol,
                        "sleeve": None,
                        "display_name": name,
                        "action": "trim_unmanaged",
                        "bucket": "sell",
                        "urgent": "trim_unmanaged" in URGENT_ACTIONS,
                        "asset_class": asset_class,
                        "confidence": None,
                        "suggested_pct": CONCENTRATION_THRESHOLD,
                        "suggested_eur": round(target_eur, 2),
                        "held_eur": round(held_eur, 2),
                        "fee": fee_info,
                        "fee_pct_of_trade": round(fee_pct, 4),
                        "fee_efficient": fee_pct < 0.01,
                        "rationale": rationale,
                        "thesis": None,
                        "correlated_holdings": [],
                        "cash_available": None,
                        "reference_price": None,
                        "limit_price": None,
                        "stop_price": None,
                        "stop_condition": None,
                        "target_price": None,
                    }
                )
                continue

        rationale = "Held, but not currently covered by the model's scan universe -- no active thesis to evaluate this against."
        if macro_line:
            rationale += " " + macro_line
        actions.append(
            {
                "symbol": symbol,
                "sleeve": None,
                "display_name": name,
                "action": "hold_unmanaged",
                "bucket": "hold",
                "urgent": "hold_unmanaged" in URGENT_ACTIONS,
                "asset_class": asset_class,
                "confidence": None,
                "suggested_pct": None,
                "suggested_eur": None,
                "held_eur": round(held_eur, 2),
                "fee": None,
                "fee_pct_of_trade": None,
                "fee_efficient": None,
                "rationale": rationale,
                "thesis": None,
                "correlated_holdings": [],
                "cash_available": None,
                "reference_price": None,
                "limit_price": None,
                "stop_price": None,
                "stop_condition": None,
                "target_price": None,
            }
        )

    priority = {"exit": 0, "trim": 1, "trim_unmanaged": 1, "add": 2, "new_entry": 3, "hold": 4, "hold_unmanaged": 5}
    actions.sort(key=lambda a: (priority[a["action"]], -(a["confidence"] or 0)))

    # Cash gate: a buy is only actually actionable if there's real deployable cash to fund
    # it -- see app/portfolio.available_cash_eur(). Greedily funds the strongest ideas
    # first (this sort order is already "most important buy first"), rather than thinning
    # every idea proportionally, so cash doesn't get spread across a pile of fee-inefficient
    # micro-positions. Once one candidate can't be fully funded, it and everything after it
    # (in this same priority order) is reclassified -- not skipped over in favor of a
    # smaller one further down the list.
    remaining_cash = available_cash["total_eur"]
    cash_exhausted = False
    for a in actions:
        if a["bucket"] != "buy":
            continue
        cash_needed = (a["suggested_eur"] or 0) - a["held_eur"]
        if not cash_exhausted and cash_needed <= remaining_cash:
            remaining_cash -= cash_needed
            a["cash_available"] = True
        else:
            cash_exhausted = True
            a["bucket"] = "buy_no_cash"
            a["cash_available"] = False
            a["rationale"] += (
                " No cash currently available to fund this -- shown for research, not "
                "actionable until cash is added (see the Portfolio section)."
            )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nav_eur": round(nav_eur, 2),
        "unpriced_symbols": unpriced_symbols,
        "macro": macro_snapshot,
        "asset_allocation": _asset_allocation(priced),
        "available_cash_eur": round(available_cash["total_eur"], 2),
        "actions": actions,
    }
