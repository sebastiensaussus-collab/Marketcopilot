"""Interactive Brokers connector. Read-only: positions and account summary only.

This never places, modifies, or cancels an order -- no code path here does anything but
read. The connection itself is also opened with readonly=True, which makes TWS/IB Gateway
reject any order-related request at the API level as a second line of defense.

You authenticate to IB Gateway or TWS yourself, through its own native window, with your
own IBKR credentials. This code only opens a local socket to that already-authenticated
process (see app.settings for host/port) -- it never sees, stores, or handles your IBKR
login. If Gateway/TWS isn't running or API access isn't enabled in its settings, every
function here just returns None rather than erroring the rest of the app.
"""

import logging

from ib_async import IB

from app.connectors.yfinance_client import _call_with_timeout
from app.settings import settings

logger = logging.getLogger("market_copilot.ibkr")

CONNECT_TIMEOUT = 5

# ib.connect() already had a timeout (above) -- reqContractDetails()/portfolio()/
# accountSummary() didn't, and live-verified today: a request can connect fine, sync
# fine, then hang forever on one of these with zero log output and no way out, because
# nothing here ever gave up waiting. Same class of bug as yfinance's rate-limit hangs
# (see yfinance_client's docstring) -- reusing its exact fix (a wall-clock thread-join
# timeout) rather than inventing a second pattern for the same problem. Cross-connector
# reuse is already established here: app/connectors/fx.py imports the same helper.
CALL_TIMEOUT = 10

# TWS/Gateway error 326 means the clientId is already held by another live connection --
# a stray debug script, a crashed-but-not-yet-timed-out previous run, or a genuine second
# app instance. Live-verified today: this silently drops every IBKR position from NAV on
# every request until whatever's squatting on the id disconnects, with no indication to
# the user beyond a quiet log line. Distinct from Gateway simply not being reachable at
# all, where retrying with a different id can't help and would only add latency.
CLIENT_ID_IN_USE_ERROR_CODE = 326
CLIENT_ID_FALLBACK_ATTEMPTS = 3


def _connect_once(client_id: int):
    """Returns (ib_or_None, was_client_id_conflict). Hooks errorEvent rather than
    inspecting the raised exception because ib_async surfaces a conflict and a genuine
    "Gateway not reachable" as the same generic TimeoutError -- the error code only
    arrives via the callback, before connect() gives up and raises.
    """
    ib = IB()
    client_id_in_use = False

    def _on_error(reqId, errorCode, errorString, contract=None):
        nonlocal client_id_in_use
        if errorCode == CLIENT_ID_IN_USE_ERROR_CODE:
            client_id_in_use = True

    ib.errorEvent += _on_error
    try:
        ib.connect(
            settings.ibkr_host,
            settings.ibkr_port,
            clientId=client_id,
            timeout=CONNECT_TIMEOUT,
            readonly=True,
        )
        return ib, False
    except Exception as exc:
        if not client_id_in_use:
            logger.info("IBKR not reachable at %s:%s (%s)", settings.ibkr_host, settings.ibkr_port, exc)
        return None, client_id_in_use
    finally:
        ib.errorEvent -= _on_error


def _connect():
    client_id = settings.ibkr_client_id
    for attempt in range(CLIENT_ID_FALLBACK_ATTEMPTS + 1):
        ib, was_conflict = _connect_once(client_id)
        if ib is not None:
            if attempt > 0:
                logger.info("IBKR connected on fallback clientId %s (configured id %s was in use)", client_id, settings.ibkr_client_id)
            return ib
        if not was_conflict:
            return None  # genuinely unreachable -- a different id won't fix that
        logger.info("IBKR clientId %s already in use, retrying with clientId %s", client_id, client_id + 1)
        client_id += 1
    logger.info("IBKR clientId %s and %s fallback ids all in use", settings.ibkr_client_id, CLIENT_ID_FALLBACK_ATTEMPTS)
    return None


def _resolve_name(ib, contract) -> str | None:
    """The bare ticker alone isn't enough to know what a position actually is -- some
    tickers are reused across genuinely different products on different exchanges (found
    live: "EUNA" is both a bond ETF and an equity-index ETF depending on listing). IBKR's
    own contract details are the authoritative source for what's actually held, so this
    is preferred over guessing from the symbol. Falls back to None (caller uses the bare
    symbol) rather than block the whole positions fetch on one lookup failing.
    """
    details = _call_with_timeout(lambda: ib.reqContractDetails(contract), timeout=CALL_TIMEOUT)
    return details[0].longName or None if details else None


def fetch_positions() -> list[dict] | None:
    """Returns current portfolio positions with live market value/P&L, or None if
    Gateway/TWS isn't reachable."""
    ib = _connect()
    if ib is None:
        return None

    try:
        return [
            {
                "symbol": item.contract.symbol,
                "name": _resolve_name(ib, item.contract) or item.contract.symbol,
                "sec_type": item.contract.secType,
                "currency": item.contract.currency,
                "exchange": item.contract.exchange or item.contract.primaryExchange,
                "position": item.position,
                "average_cost": item.averageCost,
                "market_price": item.marketPrice,
                "market_value": item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "account": item.account,
            }
            for item in ib.portfolio()
        ]
    finally:
        ib.disconnect()


ACCOUNT_SUMMARY_TAGS = {"NetLiquidation", "TotalCashValue", "GrossPositionValue", "BuyingPower"}


def fetch_account_summary() -> dict | None:
    """Returns a handful of top-level account figures, or None if unreachable."""
    ib = _connect()
    if ib is None:
        return None

    try:
        summary = {}
        for value in ib.accountSummary():
            if value.tag in ACCOUNT_SUMMARY_TAGS:
                summary[value.tag] = {"value": value.value, "currency": value.currency}
        return summary
    finally:
        ib.disconnect()
