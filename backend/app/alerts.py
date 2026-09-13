"""Urgent, standalone email alerts -- distinct from the 3x/day digest reports
(app/reports.py). Runs on its own tighter cadence (see app/main.py's urgent_alerts job).

Zero new detection logic, zero new model API cost: reuses exactly what the digest reports
already compute -- app/watchdog.py's price_move_check, and app/portfolio_risk.py's
concentration flags (a holding past the 25% threshold currently only surfaces as a
trim_unmanaged dashboard action, never as an alert).

Dedup is the load-bearing piece here, not the detection -- see AlertLog in app/store.py.
Without it, a condition that persists across many check intervals (a price move that
stays past threshold for hours) would re-fire a new email every single check, which is
the opposite of "urgent and direct." Each (alert_type, symbol) only re-alerts after
alert_cooldown_hours, whether or not the condition has actually cleared in between --
simpler than edge-triggering, and the AlertLog table is the only state needed.
"""

import logging
from datetime import datetime, timedelta, timezone

from app import portfolio_risk, watchdog
from app.notify import send_email
from app.settings import settings
from app.store import get_last_alert_sent_at, record_alert_sent

logger = logging.getLogger("market_copilot.alerts")


def _within_cooldown(alert_type: str, symbol: str) -> bool:
    last_sent = get_last_alert_sent_at(alert_type, symbol)
    if last_sent is None:
        return False
    # SQLite round-trips a stored datetime as naive but the clock value is UTC (it was
    # written via datetime.now(timezone.utc)) -- .replace(), not .astimezone(), matches
    # the same convention already used for QuoteCache staleness checks elsewhere in
    # app/store.py.
    last_sent = last_sent.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_sent < timedelta(hours=settings.alert_cooldown_hours)


def _price_move_items(price_moves: list[dict]) -> list[dict]:
    items = []
    for m in price_moves:
        if _within_cooldown("price_move", m["symbol"]):
            continue
        items.append(
            {
                "alert_type": "price_move",
                "symbol": m["symbol"],
                "line": f"[PRICE MOVE] {m['symbol']} — {m['move'] * 100:+.1f}% since thesis "
                f"(now {m['current_price']:.2f})",
            }
        )
    return items


def _concentration_items(concentration_flags: list[dict], names: dict) -> list[dict]:
    items = []
    for c in concentration_flags:
        if _within_cooldown("concentration", c["symbol"]):
            continue
        name = names.get(c["symbol"], c["symbol"])
        items.append(
            {
                "alert_type": "concentration",
                "symbol": c["symbol"],
                "line": f"[CONCENTRATION] {c['symbol']} ({name}) — {round(c['weight'] * 100)}% of priced portfolio",
            }
        )
    return items


def _build_email(items: list[dict]) -> tuple[str, str, str]:
    subject = f"Market Copilot — {len(items)} alert{'s' if len(items) != 1 else ''} need a look"

    text = "\n".join(it["line"] for it in items)
    text += "\n\nResearch decision-support, not a directive -- you place every trade."

    rows_html = "".join(
        f'<div style="padding:6px 0; border-bottom:1px solid #f0efeb; font-size:14px;">{it["line"]}</div>'
        for it in items
    )
    html = f"""
    <div style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 500px; margin: 0 auto; color: #12202f;">
      <div style="background: #9c3b3b; padding: 14px 20px; border-radius: 3px 3px 0 0;">
        <span style="color: #fff; font-size: 15px; font-weight: 700; letter-spacing: 0.03em;">MARKET COPILOT ALERT</span>
      </div>
      <div style="border: 1px solid #d9dce1; border-top: none; padding: 14px 20px; background: #ffffff;">
        {rows_html}
      </div>
      <p style="color: #97a0ab; font-size: 11px; padding: 10px 4px; font-style: italic;">
        Research decision-support, not a directive — you place every trade.
      </p>
    </div>
    """
    return subject, html, text


def check_and_send_alerts(
    *,
    price_moves: list[dict] | None = None,
    concentration_flags: list[dict] | None = None,
    names: dict | None = None,
) -> dict:
    """Injectable args are for testing without live network/DB -- mirrors
    app/action_plan.py's build_today_actions(...) pattern.
    """
    if price_moves is None:
        price_moves = watchdog.price_move_check()
    if concentration_flags is None or names is None:
        risk = portfolio_risk.compute_portfolio_risk()
        if concentration_flags is None:
            concentration_flags = risk["concentration"]["concentration_flags"]
        if names is None:
            names = risk["names"]

    items = _price_move_items(price_moves) + _concentration_items(concentration_flags, names)

    if not items:
        return {"sent": False, "items": []}

    subject, html, text = _build_email(items)
    sent = send_email(subject, html, text)
    if sent:
        # Only mark items as alerted if the email actually went out -- a failed/skipped
        # send (email not configured, SMTP error) must not silently swallow a real
        # condition into the cooldown window with nothing ever having reached the inbox.
        for it in items:
            record_alert_sent(it["alert_type"], it["symbol"])
    return {"sent": sent, "items": items}
