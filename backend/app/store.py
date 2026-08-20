import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.settings import settings


class QuoteCache(SQLModel, table=True):
    """Resolved price/name per symbol, cached briefly (minutes, not hours -- see
    get_fresh_quote_cache's default). app/portfolio_risk.py's manual-holdings pricing does
    a multi-suffix disambiguation search per symbol (see its own docstring for why that's
    necessary), and the dashboard fires /portfolio, /portfolio/risk, and /action-plan
    independently on every load -- each redoing that same search for the same holdings
    seconds apart was the single biggest contributor to a slow dashboard. This doesn't
    change what gets resolved, just how often the live lookup actually has to run.

    found=False caches a genuine "nothing resolved" (e.g. SAAB) too -- a symbol that
    fails all five suffix candidates was otherwise redoing all five failed lookups on
    every single request with no memory of having already tried."""

    symbol: str = Field(primary_key=True)
    found: bool = True
    price: Optional[float] = None
    currency: Optional[str] = None
    name: Optional[str] = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InsiderSignalCache(SQLModel, table=True):
    """Insider Form 4 cluster-buying signal per ticker, cached ~24h -- Form 4 filings are
    slow to pull (multiple SEC requests per filing) and don't change intraday, so there's
    no reason to redo this on every single refresh."""

    symbol: str = Field(primary_key=True)
    num_buy_transactions: int = 0
    num_sell_transactions: int = 0
    num_distinct_buyers: int = 0
    num_distinct_sellers: int = 0
    net_value_usd: float = 0.0
    cluster_buy_signal: bool = False
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Opportunity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    sleeve: str  # "satellite" (conviction) -- generic column, kept even though the app
    # now only ever synthesizes one sleeve
    symbol: str
    display_name: str
    content_hash: str  # hash of the synthesis input bundle, used to skip redundant Claude calls

    thesis: str
    catalysts_json: str  # JSON-encoded list[str]
    risks_json: str  # JSON-encoded list[str]
    confidence: float
    invalidation_condition: str
    sizing_note: str
    raw_metrics_json: str  # JSON-encoded dict of the calculator outputs behind this thesis

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JournalEntry(SQLModel, table=True):
    """One row per freshly-synthesized thesis (not re-created on cache hits), snapshotting
    price/confidence at creation so outcomes can be checked at fixed horizons later.

    Outcomes measure price return and excess return vs SPY, since these theses are
    directional.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    sleeve: str
    symbol: str
    thesis_snapshot: str
    confidence_at_creation: float
    price_at_creation: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    return_14d: Optional[float] = None
    checked_14d_at: Optional[datetime] = None
    return_30d: Optional[float] = None
    checked_30d_at: Optional[datetime] = None
    return_90d: Optional[float] = None
    checked_90d_at: Optional[datetime] = None

    # Satellite-only: return relative to SPY over the same window.
    excess_return_14d: Optional[float] = None
    excess_return_30d: Optional[float] = None
    excess_return_90d: Optional[float] = None


class ManualHolding(SQLModel, table=True):
    """A position imported from a broker with no API (ING, Bolero). Re-uploading a CSV
    for a broker replaces all of that broker's rows -- see replace_manual_holdings."""

    id: Optional[int] = Field(default=None, primary_key=True)
    broker: str
    symbol: str
    quantity: float
    average_cost: Optional[float] = None
    imported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BrokerCash(SQLModel, table=True):
    """User-declared available cash for a broker with no live feed (ING, Bolero) -- unlike
    IBKR's TotalCashValue (fetched live via app/connectors/ibkr.py), there's no way to
    detect this automatically, so it's the literal mechanism for "I have new cash to
    deploy": updated explicitly via POST /portfolio/cash, read by
    app/portfolio.available_cash_eur() to gate buy recommendations in app/action_plan.py."""

    broker: str = Field(primary_key=True)
    cash_eur: float
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AlertLog(SQLModel, table=True):
    """One row per actually-sent urgent alert, keyed by (alert_type, symbol) -- the dedup
    record app/alerts.py's cooldown check reads. Without this, a condition that persists
    (a funding flip that stays flipped for hours) would re-fire a new email every single
    check interval instead of once -- the opposite of "urgent and direct." Deliberately
    separate from ReportLog: alerts and the 3x/day digest reports are sent on completely
    different cadences and have no overlap in what they're deduping."""

    id: Optional[int] = Field(default=None, primary_key=True)
    alert_type: str  # "price_move" | "concentration"
    symbol: str
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportLog(SQLModel, table=True):
    """One row per successfully-sent scheduled report -- lets the scheduler tell "the Mac
    was asleep and this genuinely never fired" apart from "this already went out." The
    in-memory APScheduler job store doesn't survive a process restart, and a laptop that
    sleeps overnight silently skips a cron misfire by default; this is the durable record
    a startup catch-up check reads (see main.py) to decide whether to fire a missed
    report immediately instead of quietly losing it until the next scheduled time."""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # "morning" | "lunch" | "evening"
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        # check_same_thread=False + a busy timeout: /refresh synthesizes candidates
        # concurrently across a thread pool, so writes can arrive from multiple worker
        # threads at once. SQLite only allows one writer at a time; the timeout makes
        # a thread wait for the lock instead of immediately raising "database is locked".
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        SQLModel.metadata.create_all(_engine)
    return _engine


def get_fresh_quote_cache(symbol: str, max_age_seconds: int = 300) -> Optional[QuoteCache]:
    with Session(get_engine()) as session:
        entry = session.get(QuoteCache, symbol)
        if entry is None:
            return None
        computed_at = entry.computed_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - computed_at > timedelta(seconds=max_age_seconds):
            return None
        return entry


def upsert_quote_cache(
    *, symbol: str, found: bool = True, price: Optional[float] = None, currency: Optional[str] = None, name: Optional[str] = None
) -> QuoteCache:
    with Session(get_engine()) as session:
        entry = session.get(QuoteCache, symbol)
        if entry is None:
            entry = QuoteCache(symbol=symbol, found=found, price=price, currency=currency, name=name)
        else:
            entry.found = found
            entry.price = price
            entry.currency = currency
            entry.name = name
            entry.computed_at = datetime.now(timezone.utc)
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry


def get_fresh_insider_signal_cache(symbol: str, max_age_hours: int = 24) -> Optional[InsiderSignalCache]:
    with Session(get_engine()) as session:
        entry = session.get(InsiderSignalCache, symbol)
        if entry is None:
            return None
        computed_at = entry.computed_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - computed_at > timedelta(hours=max_age_hours):
            return None
        return entry


def upsert_insider_signal_cache(*, symbol: str, summary: dict) -> InsiderSignalCache:
    with Session(get_engine()) as session:
        entry = session.get(InsiderSignalCache, symbol)
        if entry is None:
            entry = InsiderSignalCache(symbol=symbol)
        entry.num_buy_transactions = summary["num_buy_transactions"]
        entry.num_sell_transactions = summary["num_sell_transactions"]
        entry.num_distinct_buyers = summary["num_distinct_buyers"]
        entry.num_distinct_sellers = summary["num_distinct_sellers"]
        entry.net_value_usd = summary["net_value_usd"]
        entry.cluster_buy_signal = summary["cluster_buy_signal"]
        entry.computed_at = datetime.now(timezone.utc)
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry


def find_cached(symbol: str, sleeve: str, content_hash: str) -> Optional[Opportunity]:
    with Session(get_engine()) as session:
        statement = select(Opportunity).where(
            Opportunity.symbol == symbol,
            Opportunity.sleeve == sleeve,
            Opportunity.content_hash == content_hash,
        )
        return session.exec(statement).first()


def upsert_opportunity(
    *,
    sleeve: str,
    symbol: str,
    display_name: str,
    content_hash: str,
    thesis: str,
    catalysts: list[str],
    risks: list[str],
    confidence: float,
    invalidation_condition: str,
    sizing_note: str,
    raw_metrics: dict,
) -> Opportunity:
    with Session(get_engine()) as session:
        statement = select(Opportunity).where(
            Opportunity.symbol == symbol, Opportunity.sleeve == sleeve
        )
        existing = session.exec(statement).first()

        if existing:
            existing.display_name = display_name
            existing.content_hash = content_hash
            existing.thesis = thesis
            existing.catalysts_json = json.dumps(catalysts)
            existing.risks_json = json.dumps(risks)
            existing.confidence = confidence
            existing.invalidation_condition = invalidation_condition
            existing.sizing_note = sizing_note
            existing.raw_metrics_json = json.dumps(raw_metrics)
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing

        opportunity = Opportunity(
            sleeve=sleeve,
            symbol=symbol,
            display_name=display_name,
            content_hash=content_hash,
            thesis=thesis,
            catalysts_json=json.dumps(catalysts),
            risks_json=json.dumps(risks),
            confidence=confidence,
            invalidation_condition=invalidation_condition,
            sizing_note=sizing_note,
            raw_metrics_json=json.dumps(raw_metrics),
        )
        session.add(opportunity)
        session.commit()
        session.refresh(opportunity)
        return opportunity


def get_opportunities(sleeve: Optional[str] = None) -> list[Opportunity]:
    with Session(get_engine()) as session:
        statement = select(Opportunity).order_by(Opportunity.confidence.desc())
        if sleeve:
            statement = statement.where(Opportunity.sleeve == sleeve)
        return list(session.exec(statement).all())


def replace_manual_holdings(broker: str, holdings: list[dict]) -> list[ManualHolding]:
    """Replaces all of `broker`'s holdings with `holdings` -- a fresh CSV upload is a full
    resnapshot, not a merge, since there's no reliable way to diff a broker export."""
    with Session(get_engine()) as session:
        existing = session.exec(select(ManualHolding).where(ManualHolding.broker == broker)).all()
        for row in existing:
            session.delete(row)
        session.commit()

        created = []
        for h in holdings:
            row = ManualHolding(
                broker=broker, symbol=h["symbol"], quantity=h["quantity"], average_cost=h.get("average_cost")
            )
            session.add(row)
            created.append(row)
        session.commit()
        for row in created:
            session.refresh(row)
        return created


def get_manual_holdings(broker: Optional[str] = None) -> list[ManualHolding]:
    with Session(get_engine()) as session:
        statement = select(ManualHolding)
        if broker:
            statement = statement.where(ManualHolding.broker == broker)
        return list(session.exec(statement).all())


def upsert_manual_trade(broker: str, symbol: str, new_quantity: float, new_average_cost: Optional[float]) -> Optional[ManualHolding]:
    """Writes the already-computed result of a single buy/sell (see
    portfolio.compute_trade_result for the arithmetic) for one (broker, symbol) row --
    unlike replace_manual_holdings this touches only that one row, not the whole broker.
    new_quantity <= 0 closes the position (row deleted) rather than leaving a zero/negative
    row behind. Returns the updated row, or None if the position was closed."""
    with Session(get_engine()) as session:
        row = session.exec(
            select(ManualHolding).where(ManualHolding.broker == broker, ManualHolding.symbol == symbol)
        ).first()

        if new_quantity <= 0:
            if row is not None:
                session.delete(row)
                session.commit()
            return None

        if row is None:
            row = ManualHolding(broker=broker, symbol=symbol, quantity=new_quantity, average_cost=new_average_cost)
        else:
            row.quantity = new_quantity
            row.average_cost = new_average_cost
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def upsert_broker_cash(broker: str, cash_eur: float) -> BrokerCash:
    with Session(get_engine()) as session:
        row = session.get(BrokerCash, broker)
        if row is None:
            row = BrokerCash(broker=broker, cash_eur=cash_eur)
        else:
            row.cash_eur = cash_eur
            row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def get_broker_cash(broker: Optional[str] = None) -> list[BrokerCash]:
    with Session(get_engine()) as session:
        statement = select(BrokerCash)
        if broker:
            statement = statement.where(BrokerCash.broker == broker)
        return list(session.exec(statement).all())


def create_journal_entry(
    *, sleeve: str, symbol: str, thesis: str, confidence: float, price: float
) -> JournalEntry:
    with Session(get_engine()) as session:
        entry = JournalEntry(
            sleeve=sleeve, symbol=symbol, thesis_snapshot=thesis, confidence_at_creation=confidence, price_at_creation=price
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry


def get_journal_entries_due(horizon_days: int, field_name: str) -> list[JournalEntry]:
    """Entries old enough for `horizon_days` whose outcome field (e.g. 'return_14d') is
    still unset -- i.e. due for review and not yet reviewed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=horizon_days)
    with Session(get_engine()) as session:
        statement = select(JournalEntry).where(
            JournalEntry.created_at <= cutoff,
            getattr(JournalEntry, field_name).is_(None),
        )
        return list(session.exec(statement).all())


def save_journal_entry(entry: JournalEntry) -> None:
    with Session(get_engine()) as session:
        session.add(entry)
        session.commit()


def get_journal_entries() -> list[JournalEntry]:
    with Session(get_engine()) as session:
        statement = select(JournalEntry).order_by(JournalEntry.created_at.desc())
        return list(session.exec(statement).all())


def record_report_sent(kind: str) -> None:
    with Session(get_engine()) as session:
        session.add(ReportLog(kind=kind))
        session.commit()


def get_last_report_sent_at(kind: str) -> Optional[datetime]:
    with Session(get_engine()) as session:
        statement = (
            select(ReportLog).where(ReportLog.kind == kind).order_by(ReportLog.sent_at.desc()).limit(1)
        )
        row = session.exec(statement).first()
        return row.sent_at if row else None


def record_alert_sent(alert_type: str, symbol: str) -> None:
    with Session(get_engine()) as session:
        session.add(AlertLog(alert_type=alert_type, symbol=symbol))
        session.commit()


def get_last_alert_sent_at(alert_type: str, symbol: str) -> Optional[datetime]:
    with Session(get_engine()) as session:
        statement = (
            select(AlertLog)
            .where(AlertLog.alert_type == alert_type, AlertLog.symbol == symbol)
            .order_by(AlertLog.sent_at.desc())
            .limit(1)
        )
        row = session.exec(statement).first()
        return row.sent_at if row else None


def journal_entry_to_api_dict(entry: JournalEntry) -> dict:
    return {
        "id": entry.id,
        "sleeve": entry.sleeve,
        "symbol": entry.symbol,
        "thesis_snapshot": entry.thesis_snapshot,
        "confidence_at_creation": entry.confidence_at_creation,
        "price_at_creation": entry.price_at_creation,
        "created_at": entry.created_at.isoformat(),
        "return_14d": entry.return_14d,
        "return_30d": entry.return_30d,
        "return_90d": entry.return_90d,
        "excess_return_14d": entry.excess_return_14d,
        "excess_return_30d": entry.excess_return_30d,
        "excess_return_90d": entry.excess_return_90d,
    }


def to_api_dict(opportunity: Opportunity) -> dict:
    return {
        "id": opportunity.id,
        "sleeve": opportunity.sleeve,
        "symbol": opportunity.symbol,
        "display_name": opportunity.display_name,
        "thesis": opportunity.thesis,
        "catalysts": json.loads(opportunity.catalysts_json),
        "risks": json.loads(opportunity.risks_json),
        "confidence": opportunity.confidence,
        "invalidation_condition": opportunity.invalidation_condition,
        "sizing_note": opportunity.sizing_note,
        "raw_metrics": json.loads(opportunity.raw_metrics_json),
        "updated_at": opportunity.updated_at.isoformat(),
    }
