import logging
import threading
from datetime import datetime
from datetime import time as dt_time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import action_plan, alerts, journal, notify, portfolio, portfolio_risk, reports, refresh as refresh_module
from app.backtest import equity_momentum
from app.settings import settings
from app.store import (
    get_journal_entries,
    get_last_report_sent_at,
    get_opportunities,
    journal_entry_to_api_dict,
    to_api_dict,
)
from app.universe import SP100

logger = logging.getLogger("market_copilot")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Market Copilot")

# A laptop that sleeps overnight silently drops a cron trigger's fire time by default --
# APScheduler's default misfire_grace_time is 1 second, so waking up even a few minutes
# late means the job is skipped, not run late (live-verified: this is exactly what
# quietly ate several scheduled reports this week). A generous grace window here covers
# "closed the lid, opened it a few hours later"; the startup catch-up check below covers
# the longer gap a generous grace window still can't (machine fully off overnight, or a
# nap that outlasts even this).
MISFIRE_GRACE_SECONDS = 6 * 3600

scheduler = BackgroundScheduler(job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS})


def _scheduled_report(kind: str):
    try:
        sent = reports.send_report(kind)
        logger.info("Scheduled %s report: %s", kind, "sent" if sent else "skipped (email not configured)")
    except Exception as exc:
        logger.exception("Scheduled %s report failed", kind)
        # Every per-candidate/per-symbol failure mode already degrades gracefully inside
        # run_full_refresh() -- this only fires for something genuinely unforeseen. In that
        # case silence is the worst outcome: nothing arrives and there's no signal anything
        # even ran. Best-effort only -- notify.send_email never raises, but this must not be
        # able to take the scheduler down either way.
        try:
            notify.send_email(
                f"Market Copilot — {kind} report FAILED",
                f"<p>The scheduled {kind} report crashed and could not be generated.</p><p>{exc}</p>",
                f"The scheduled {kind} report crashed and could not be generated.\n{exc}",
            )
        except Exception:
            logger.exception("Even the failure-notice email for %s failed", kind)


def _run_alert_check():
    try:
        result = alerts.check_and_send_alerts()
        if result["sent"]:
            logger.info("Urgent alert sent: %d item(s)", len(result["items"]))
    except Exception:
        # Deliberately does NOT send a failure-notice email (unlike _scheduled_report) --
        # this runs every alert_check_interval_minutes, so a transient failure emailing
        # about itself would become exactly the noise problem alerts exist to avoid. A
        # persistent problem shows up in the logs; the next successful tick just resumes.
        logger.exception("Urgent alert check failed")


def _report_kinds():
    return (
        ("morning", settings.morning_report_time),
        ("lunch", settings.lunch_report_time),
        ("evening", settings.evening_report_time),
    )


def _catch_up_missed_reports():
    """Startup safety net for the gap even MISFIRE_GRACE_SECONDS can't cover -- e.g. the
    Mac was fully off overnight, not just asleep. Checks each report's last successful
    send (app/store.py's ReportLog, which survives a process restart -- APScheduler's
    default in-memory job store doesn't) against today's scheduled time, and if anything
    is overdue and still missing, fires the SINGLE most recently-scheduled one -- not
    every missed kind. Live-verified why that matters: firing all three at once means a
    "Morning Brief" arriving at 10pm hours after lunch/evening already recapped the same
    picks, a real (paid) Claude refresh for a report that's already stale by the time it's
    read, and several concurrent portfolio/IBKR lookups colliding with each other for no
    benefit. The most recent missed kind is the freshest, most relevant one to catch up
    on; runs in a background thread so a ~2 minute morning refresh doesn't block the app
    from serving requests on startup. Idempotent: safe to call on every restart, since an
    already-sent-today kind is never re-fired.
    """
    now = datetime.now()
    overdue_missing = []
    for kind, time_str in _report_kinds():
        hour, minute = time_str.split(":")
        scheduled_today = datetime.combine(now.date(), dt_time(int(hour), int(minute)))
        if now < scheduled_today:
            continue  # not due yet today -- the normal cron trigger will handle it

        last_sent = get_last_report_sent_at(kind)
        if last_sent is not None and last_sent.astimezone().date() == now.date():
            continue  # already sent today

        overdue_missing.append((kind, scheduled_today))

    if not overdue_missing:
        return

    kind, _ = max(overdue_missing, key=lambda pair: pair[1])
    logger.info("Startup catch-up: %s report is overdue for today and missing -- sending now", kind)
    threading.Thread(target=_scheduled_report, args=(kind,), daemon=True).start()


@app.on_event("startup")
def start_scheduler():
    if not settings.reports_enabled and not settings.alerts_enabled:
        return

    if settings.reports_enabled:
        for kind, time_str in _report_kinds():
            hour, minute = time_str.split(":")
            scheduler.add_job(
                _scheduled_report,
                CronTrigger(hour=int(hour), minute=int(minute)),
                args=[kind],
                id=f"report_{kind}",
                replace_existing=True,
            )

    if settings.alerts_enabled:
        # max_instances=1: a slow check (live network calls across every accumulated
        # opportunity) must not pile up behind the next tick rather than overlap it.
        scheduler.add_job(
            _run_alert_check,
            IntervalTrigger(minutes=settings.alert_check_interval_minutes),
            id="urgent_alerts",
            replace_existing=True,
            max_instances=1,
        )

    scheduler.start()
    logger.info(
        "Report scheduler started: morning=%s lunch=%s evening=%s, urgent_alerts=%s (every %dmin)",
        settings.morning_report_time,
        settings.lunch_report_time,
        settings.evening_report_time,
        settings.alerts_enabled,
        settings.alert_check_interval_minutes,
    )
    if settings.reports_enabled:
        _catch_up_missed_reports()


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

# Tauri's webview and plain `vite dev` both run on localhost with a different origin
# than the backend; this is a local-only app so a permissive local CORS policy is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "tauri://localhost", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/refresh")
def refresh():
    """Runs the screener, synthesizes the shortlist, and returns run stats.
    This is what the dashboard's Refresh button (and the morning report) call.
    See app/refresh.py for the concurrency/cost-control rationale.
    """
    return refresh_module.run_full_refresh()


@app.get("/opportunities")
def opportunities(sleeve: str | None = None):
    return {
        "satellite": [to_api_dict(o) for o in get_opportunities("satellite")]
        if sleeve in (None, "satellite")
        else [],
    }


@app.get("/journal")
def journal_view():
    """Every thesis ever synthesized, with outcomes at 14/30/90-day horizons once matured,
    plus a confidence-vs-actual-hit-rate calibration summary. Empty/sparse until entries
    actually age past a horizon -- there's no way to shortcut real time passing.
    """
    entries = [journal_entry_to_api_dict(e) for e in get_journal_entries()]
    return {"entries": entries, "calibration": journal.calibration_summary()}


@app.post("/journal/review")
def journal_review():
    """Manually trigger a review pass without waiting for the next /refresh."""
    return journal.review_due_entries()


@app.get("/portfolio")
def portfolio_view():
    """Unified view across IBKR (read-only, live) and manually-imported ING/Bolero
    holdings. `ibkr.connected: false` just means Gateway/TWS isn't reachable right now --
    that's the expected state until it's installed and running with API access enabled.
    """
    return portfolio.get_unified_portfolio()


@app.get("/action-plan")
def action_plan_view():
    """Today's tailored, portfolio-aware trade shortlist: synthesized opportunities
    cross-referenced against what's actually held, netted against estimated broker
    fees/Belgian TOB, with invalidated theses flagged for exit. Still research
    decision-support, never a directive -- see app/action_plan.py. Zero new Claude cost,
    pure arithmetic over data already fetched elsewhere.
    """
    return action_plan.build_today_actions()


@app.get("/portfolio/risk")
def portfolio_risk_view():
    """Value-weighted concentration and correlation across everything actually held.
    Correlation is only computed for symbols where yfinance returns a real history --
    some listings only return a live quote, not a `.history()` series. Excluded symbols
    are reported explicitly. See app/portfolio_risk.py for the full rationale.
    """
    return portfolio_risk.compute_portfolio_risk()


@app.post("/portfolio/import")
async def portfolio_import(broker: str, file: UploadFile = File(...)):
    """Replaces `broker`'s holdings with the uploaded CSV (columns: symbol, quantity,
    optional average_cost). Re-uploading fully replaces that broker's prior holdings."""
    content = (await file.read()).decode("utf-8")
    try:
        count = portfolio.import_csv(broker, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"broker": broker, "holdings_imported": count}


class BrokerCashRequest(BaseModel):
    broker: str
    cash_eur: float


@app.post("/portfolio/cash")
def portfolio_set_cash(payload: BrokerCashRequest):
    """Declares available cash for a broker with no live feed (ING, Bolero) -- the
    mechanism for "I have new cash to deploy" that gates buy recommendations in
    app/action_plan.py. IBKR's cash is live/read-only and has no equivalent endpoint."""
    try:
        return portfolio.set_broker_cash(payload.broker, payload.cash_eur)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


ALLOWED_DOCUMENT_MEDIA_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}


@app.post("/portfolio/import-document")
async def portfolio_import_document(broker: str, file: UploadFile = File(...)):
    """Reads a broker statement (PDF or screenshot) via Claude and returns the extracted
    holdings for review -- does not touch the DB. See POST /portfolio/confirm-import to
    actually commit them once reviewed (symbol is a Claude guess until confirmed)."""
    if file.content_type not in ALLOWED_DOCUMENT_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")
    file_bytes = await file.read()
    try:
        holdings = portfolio.extract_holdings_from_document(file_bytes, file.content_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"broker": broker, "holdings": holdings}


class ManualHoldingIn(BaseModel):
    symbol: str
    quantity: float
    average_cost: Optional[float] = None


class ConfirmImportRequest(BaseModel):
    broker: str
    holdings: list[ManualHoldingIn]


@app.post("/portfolio/confirm-import")
def portfolio_confirm_import(payload: ConfirmImportRequest):
    """Commits a (possibly user-edited) set of holdings from the document-import review
    step, via the same full-replace-per-broker path as the CSV import."""
    holdings = [h.model_dump() for h in payload.holdings]
    count = portfolio.confirm_import(payload.broker, holdings)
    return {"broker": payload.broker, "holdings_imported": count}


class TradeRequest(BaseModel):
    broker: str
    symbol: str
    action: str
    quantity: float
    price: float


@app.post("/portfolio/trade")
def portfolio_trade(payload: TradeRequest):
    """Records a single buy/sell against one manual holding -- an in-place update to that
    one (broker, symbol) row, not a re-import. See app/portfolio.record_trade."""
    try:
        result = portfolio.record_trade(payload.broker, payload.symbol, payload.action, payload.quantity, payload.price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result or {"broker": payload.broker, "symbol": payload.symbol.upper(), "closed": True}


@app.get("/backtest/equity")
def backtest_equity(
    universe_size: int = 30,
    years: int = 3,
    holding_period_days: int = 21,
    top_n: int = 10,
):
    """Does the satellite sleeve's technical/momentum signal actually beat SPY historically?
    Fundamentals aren't included — see app/backtest/equity_momentum.py for why.
    """
    tickers = SP100[:universe_size]
    return equity_momentum.run_walk_forward(
        tickers, years=years, holding_period_days=holding_period_days, top_n=top_n
    )


@app.get("/backtest/equity/weight-sweep")
def backtest_equity_weight_sweep(
    universe_size: int = 30,
    years: int = 3,
    holding_period_days: int = 21,
    top_n: int = 10,
):
    """Are the equity screener's technical-score weights (RSI band, off-high, Sharpe --
    see TECHNICAL_SCORE_WEIGHTS in app/screener.py) actually a good combination, or just
    a hand-picked one? Fetches price histories once, then re-scores under 27 nearby
    weight combinations to see if any backtest meaningfully better than what's live
    today. A research tool -- nothing here changes live scoring automatically.
    """
    tickers = SP100[:universe_size]
    return equity_momentum.sweep_technical_weights(
        tickers, years=years, holding_period_days=holding_period_days, top_n=top_n
    )


@app.get("/reports/preview/{kind}")
def reports_preview(kind: str):
    """Returns the report's HTML without sending anything -- for checking content/design
    without waiting for the scheduled time or spamming your inbox."""
    if kind not in reports.REPORT_GENERATORS:
        raise HTTPException(status_code=404, detail=f"Unknown report kind '{kind}'")
    report = reports.REPORT_GENERATORS[kind]()
    return HTMLResponse(report["html"])


@app.post("/reports/send/{kind}")
def reports_send(kind: str):
    """Manually triggers and sends a report right now, without waiting for its scheduled time."""
    if kind not in reports.REPORT_GENERATORS:
        raise HTTPException(status_code=404, detail=f"Unknown report kind '{kind}'")
    sent = reports.send_report(kind)
    return {"kind": kind, "sent": sent, "email_configured": notify.is_configured()}
