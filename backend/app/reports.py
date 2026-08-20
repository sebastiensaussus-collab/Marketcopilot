"""Generates the three daily report emails.

Cost design: morning triggers a real refresh (screener + Claude synthesis -- the only
one that spends API calls). Lunch and evening are free-data pulse checks -- live IBKR
P&L, a recap of the morning's picks -- so three daily touchpoints cost roughly what one
refresh costs, not three.

"Invalidation watchdog" is deliberately a computable proxy, not a literal check of the
free-text invalidation_condition Claude writes (that's natural language, not machine
checkable without another LLM call per position, which would defeat the cost design
above): has price moved materially since the thesis's journal snapshot.
"""

import json
from datetime import datetime, timedelta, timezone

from app import action_plan as action_plan_module
from app import portfolio as portfolio_module
from app import refresh as refresh_module
from app import watchdog
from app.notify import send_email
from app.store import get_journal_entries, get_opportunities, record_report_sent

TOP_N_PER_SLEEVE = 4


def _top(opportunities, n=TOP_N_PER_SLEEVE):
    return sorted(opportunities, key=lambda o: o.confidence, reverse=True)[:n]


def _fmt_pct(x, digits=1):
    return f"{x * 100:.{digits}f}%" if x is not None else "n/a"


# ---------- shared email shell (brand-consistent with the app's navy/brass look) ----------

_SHELL = """\
<div style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #12202f;">
  <div style="background: #0b2545; padding: 18px 24px; border-radius: 3px 3px 0 0;">
    <span style="color: #f4f1e8; font-size: 18px; font-weight: 600;">Market Copilot</span>
    <span style="color: #9c7a29; font-size: 12px; margin-left: 10px; text-transform: uppercase; letter-spacing: 0.05em;">{label}</span>
  </div>
  <div style="border: 1px solid #d9dce1; border-top: none; padding: 20px 24px; background: #ffffff;">
    {body}
  </div>
  <p style="color: #97a0ab; font-size: 11px; padding: 12px 4px; font-style: italic;">
    Research decision-support, not a directive — you place every trade.
  </p>
</div>
"""


def _shell(label: str, body_html: str) -> str:
    return _SHELL.format(label=label, body=body_html)


def _section_html(title: str, color: str, rows_html: str) -> str:
    return f"""
    <h3 style="color: {color}; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
               border-bottom: 2px solid #e8e7e2; padding-bottom: 6px; margin: 18px 0 10px;">{title}</h3>
    {rows_html}
    """


def _suggested_size_pct(o) -> str:
    if not o.raw_metrics_json:
        return ""
    pct = json.loads(o.raw_metrics_json).get("suggested_position_pct")
    return f" | suggested size {pct * 100:.1f}%" if pct is not None else ""


def _opportunity_row_html(o) -> str:
    size_html = ""
    size_pct = _suggested_size_pct(o)
    if size_pct:
        size_html = f' <span style="color: #0b2545; font-weight: 600;">&middot;{size_pct}</span>'
    return f"""
    <div style="padding: 8px 0; border-bottom: 1px solid #f0efeb;">
      <b>{o.symbol}</b> ({o.display_name}) &mdash; <span style="color: #9c7a29;">{round(o.confidence * 100)}% confidence</span>{size_html}
      <div style="color: #5b6675; font-size: 13px; margin-top: 3px;">{o.thesis[:220]}{"…" if len(o.thesis) > 220 else ""}</div>
    </div>
    """


def _opportunity_row_text(o) -> str:
    return f"- {o.symbol} ({o.display_name}) [{round(o.confidence * 100)}%]{_suggested_size_pct(o)}: {o.thesis[:200]}"


# ---------- today's actions (portfolio + fee + macro aware) ----------

_ACTION_COLORS = {"exit": "#9c3b3b", "trim": "#9c7a29", "add": "#1f6b45", "new_entry": "#0b2545"}
_ACTION_LABELS = {"exit": "EXIT", "trim": "TRIM", "add": "ADD", "new_entry": "NEW"}


def _fee_note(a: dict) -> str:
    fee = a["fee"]
    note = f"est. fee €{fee['fee_eur']:.2f}"
    if fee.get("tob_eur"):
        note += f" + €{fee['tob_eur']:.2f} TOB"
    return note + " (estimated, not your actual broker terms)"


def _levels_note(a: dict) -> str:
    parts = []
    if a.get("limit_price") is not None:
        parts.append(f"limit ~{a['limit_price']:.2f}")
    if a.get("stop_price") is not None:
        parts.append(f"stop ~{a['stop_price']:.2f}")
    if a.get("stop_condition"):
        parts.append(a["stop_condition"])
    if a.get("target_price") is not None:
        parts.append(f"DCF fair value ~{a['target_price']:.2f}")
    return " · ".join(parts) + " — suggested reference levels, not orders placed" if parts else ""


def _action_row_html(a: dict) -> str:
    color = _ACTION_COLORS.get(a["action"], "#5b6675")
    label = _ACTION_LABELS.get(a["action"], a["action"].upper())
    eur = f"€{a['suggested_eur']:.0f}" if a["suggested_eur"] is not None else "n/a"
    levels = _levels_note(a)
    levels_html = f'<div style="color:#97a0ab; font-size:11px; margin-top:2px;">{levels}</div>' if levels else ""
    return f"""
    <div style="padding: 8px 0; border-bottom: 1px solid #f0efeb;">
      <span style="display:inline-block; padding:1px 6px; border-radius:3px; background:{color}; color:#fff; font-size:11px; font-weight:600;">{label}</span>
      <b style="margin-left:6px;">{a['symbol']}</b> ({a['display_name']}) &mdash; {eur}
      <div style="color:#5b6675; font-size:13px; margin-top:3px;">{a['rationale']}</div>
      <div style="color:#97a0ab; font-size:11px; margin-top:2px;">{_fee_note(a)}</div>
      {levels_html}
    </div>
    """


def _action_row_text(a: dict) -> str:
    eur = f"€{a['suggested_eur']:.0f}" if a["suggested_eur"] is not None else "n/a"
    levels = _levels_note(a)
    suffix = f" [{levels}]" if levels else ""
    return f"- [{a['action'].upper()}] {a['symbol']} ({a['display_name']}) {eur}: {a['rationale']} ({_fee_note(a)}){suffix}"


def _today_actions_section(plan: dict) -> tuple[str, list[str]]:
    # Email digest stays to Buy/Sell -- Hold is "nothing to do," which belongs on the
    # always-available dashboard, not repeated in every inbox message (same "don't
    # clutter the email" discipline as the rest of this module).
    tradeable = [a for a in plan["actions"] if a["bucket"] in ("buy", "sell")]

    nav_line_html = (
        f"<p style='color:#5b6675; font-size:12px;'>Priced portfolio NAV: €{plan['nav_eur']:.0f} "
        f"&mdash; €{plan['available_cash_eur']:.0f} cash available to deploy"
    )
    if plan["unpriced_symbols"]:
        nav_line_html += f" &mdash; unpriced (excluded): {', '.join(plan['unpriced_symbols'])}"
    nav_line_html += "</p>"
    cash_line_text = f"Cash available to deploy: €{plan['available_cash_eur']:.0f}"

    if not tradeable:
        html = _section_html(
            "Today's actions",
            "#0b2545",
            nav_line_html + "<p>Nothing actionable today — current positions sit within model-suggested weight and no new idea clears the bar.</p>",
        )
        text = [
            "TODAY'S ACTIONS:",
            "  none — positions within model-suggested weight, no new idea clears the bar.",
            cash_line_text,
        ]
        return html, text

    rows_html = "".join(_action_row_html(a) for a in tradeable[:8])
    html = _section_html("Today's actions", "#0b2545", nav_line_html + rows_html)
    text = ["TODAY'S ACTIONS:", *[_action_row_text(a) for a in tradeable[:8]], cash_line_text]
    return html, text


# ---------- morning ----------


def generate_morning_report() -> dict:
    stats = refresh_module.run_full_refresh()

    satellite = _top(get_opportunities("satellite"))
    portfolio_snapshot = portfolio_module.get_unified_portfolio()

    yesterday = datetime.now(timezone.utc) - timedelta(hours=18)
    overnight_matured = [
        e
        for e in get_journal_entries()
        if any(
            getattr(e, f"checked_{h}d_at") and getattr(e, f"checked_{h}d_at").replace(tzinfo=timezone.utc) >= yesterday
            for h in (14, 30, 90)
        )
    ]

    plan = action_plan_module.build_today_actions()
    actions_html, actions_text = _today_actions_section(plan)

    body = f"""
    <p>Scanned {stats['equity_candidates_scanned']} equity candidates. Synthesized
    {stats['satellite_opportunities_synthesized']} opportunities.</p>
    {actions_html}
    {_section_html("Top picks", "#9c7a29", "".join(_opportunity_row_html(o) for o in satellite) or "<p>None yet.</p>")}
    {_portfolio_section_html(portfolio_snapshot)}
    """
    if overnight_matured:
        body += _section_html(
            "Overnight journal outcomes",
            "#1f6b45",
            "".join(f"<p>{e.symbol}: thesis from {e.created_at.date()} just matured a horizon.</p>" for e in overnight_matured),
        )

    text_lines = [
        f"Scanned {stats['equity_candidates_scanned']} equity candidates.",
        f"Synthesized {stats['satellite_opportunities_synthesized']} opportunities.",
        "",
        *actions_text,
        "",
        "TOP PICKS:",
        *[_opportunity_row_text(o) for o in satellite],
    ]

    return {
        "subject": f"Market Copilot — Morning Brief, {datetime.now().strftime('%b %d')}",
        "html": _shell("Morning Brief", body),
        "text": "\n".join(text_lines),
    }


# ---------- lunch / evening (shared pulse-check logic) ----------


def _portfolio_section_html(snapshot: dict) -> str:
    rows = ""
    if snapshot["ibkr"]["connected"]:
        for p in snapshot["ibkr"]["positions"]:
            pnl_color = "#1f6b45" if p["unrealized_pnl"] >= 0 else "#9c3b3b"
            rows += (
                f'<div style="padding: 4px 0;">{p["symbol"]}: {p["position"]} @ '
                f'{p["market_price"]:.2f} &mdash; <span style="color: {pnl_color};">'
                f'P&amp;L {p["unrealized_pnl"]:+.2f}</span></div>'
            )
    else:
        rows = "<p>IBKR not connected.</p>"
    return _section_html("Portfolio (IBKR live)", "#0b2545", rows)


def _pulse_body_and_text(heading_extra_html: str = "", heading_extra_text: str = "") -> tuple[str, list[str]]:
    moves = watchdog.price_move_check()
    snapshot = portfolio_module.get_unified_portfolio()

    plan = action_plan_module.build_today_actions(price_moves=moves)
    actions_html, actions_text = _today_actions_section(plan)

    body = heading_extra_html + actions_html

    if moves:
        rows = "".join(
            f'<div style="padding: 4px 0;">{m["symbol"]}: {_fmt_pct(m["move"])} since thesis '
            f'(now {m["current_price"]:.2f})</div>'
            for m in moves
        )
        body += _section_html("Notable price moves since thesis", "#9c7a29", rows)
    else:
        body += "<p>No notable price moves since the last check.</p>"

    body += _portfolio_section_html(snapshot)

    text_lines = [heading_extra_text] if heading_extra_text else []
    text_lines += actions_text
    text_lines.append("")
    text_lines.append("PRICE MOVES:")
    text_lines += [f"- {m['symbol']}: {_fmt_pct(m['move'])} since thesis" for m in moves] or ["  none"]

    return body, text_lines


def generate_lunch_report() -> dict:
    satellite_recap = _top(get_opportunities("satellite"), 3)

    recap_html = _section_html(
        "This morning's top picks (recap)",
        "#0b2545",
        "".join(_opportunity_row_html(o) for o in satellite_recap) or "<p>No opportunities yet today.</p>",
    )

    body, text_lines = _pulse_body_and_text(recap_html, "")

    return {
        "subject": f"Market Copilot — Midday Check, {datetime.now().strftime('%b %d')}",
        "html": _shell("Midday Check", body),
        "text": "\n".join(text_lines),
    }


def generate_evening_report() -> dict:
    from app import journal as journal_module

    reviewed = journal_module.review_due_entries()
    total_reviewed = sum(reviewed.values())

    body, text_lines = _pulse_body_and_text()
    body += f"<p style='color: #5b6675; font-size: 13px;'>Journal review: {total_reviewed} entries checked against a horizon today.</p>"
    body += "<p style='color: #5b6675; font-size: 13px;'>Fresh scan runs in the morning.</p>"

    text_lines.append(f"Journal review: {total_reviewed} entries checked today.")

    return {
        "subject": f"Market Copilot — Evening Wrap, {datetime.now().strftime('%b %d')}",
        "html": _shell("Evening Wrap", body),
        "text": "\n".join(text_lines),
    }


# ---------- dispatch ----------

REPORT_GENERATORS = {
    "morning": generate_morning_report,
    "lunch": generate_lunch_report,
    "evening": generate_evening_report,
}


def send_report(kind: str) -> bool:
    report = REPORT_GENERATORS[kind]()
    sent = send_email(report["subject"], report["html"], report["text"])
    if sent:
        record_report_sent(kind)
    return sent
