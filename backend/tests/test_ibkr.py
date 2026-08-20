import time
from types import SimpleNamespace

from eventkit import Event

import app.connectors.ibkr as ibkr_module
from app.connectors.ibkr import _resolve_name


def test_resolve_name_returns_authoritative_long_name():
    # The bare ticker alone isn't enough -- some symbols are reused across genuinely
    # different products on different exchanges (found live: "EUNA" is both a bond ETF
    # and an equity-index ETF depending on listing). IBKR's own contract details are the
    # ground truth for what's actually held.
    fake_details = [SimpleNamespace(longName="ISHARES GLB AGG EUR-H ACC")]
    fake_ib = SimpleNamespace(reqContractDetails=lambda contract: fake_details)

    name = _resolve_name(fake_ib, contract=SimpleNamespace(symbol="EUNA"))

    assert name == "ISHARES GLB AGG EUR-H ACC"


def test_resolve_name_none_when_lookup_fails():
    def boom(contract):
        raise ConnectionError("no response")

    fake_ib = SimpleNamespace(reqContractDetails=boom)

    assert _resolve_name(fake_ib, contract=SimpleNamespace(symbol="XYZ")) is None


def test_resolve_name_none_when_no_details_returned():
    fake_ib = SimpleNamespace(reqContractDetails=lambda contract: [])
    assert _resolve_name(fake_ib, contract=SimpleNamespace(symbol="XYZ")) is None


def test_resolve_name_returns_none_quickly_when_reqContractDetails_hangs(monkeypatch):
    # The actual bug this fixes, live-verified: a request connected to IBKR fine, synced
    # fine, then hung forever on reqContractDetails with zero log output and no escape --
    # unlike ib.connect() (CONNECT_TIMEOUT), this call had no timeout at all before.
    monkeypatch.setattr(ibkr_module, "CALL_TIMEOUT", 0.2)

    def hangs(contract):
        time.sleep(5)
        return [SimpleNamespace(longName="too late")]

    fake_ib = SimpleNamespace(reqContractDetails=hangs)

    started = time.time()
    name = _resolve_name(fake_ib, contract=SimpleNamespace(symbol="EUNA"))
    elapsed = time.time() - started

    assert name is None
    assert elapsed < 1  # bounded by CALL_TIMEOUT, not by how long reqContractDetails takes


class _FakeIB:
    """Stands in for ib_async.IB in _connect() tests -- real errorEvent is an eventkit
    Event, so this uses the real Event class rather than a hand-rolled callback list, to
    stay faithful to the += / emit() contract _connect_once() actually depends on."""

    def __init__(self, on_connect):
        self.errorEvent = Event()
        self._on_connect = on_connect
        self.disconnected = False

    def connect(self, host, port, clientId, timeout, readonly):
        self._on_connect(self, clientId)

    def disconnect(self):
        self.disconnected = True


def test_connect_retries_with_fallback_client_id_on_conflict(monkeypatch):
    # The actual bug this fixes, live-verified: a stray process squatting on the
    # configured clientId silently drops every IBKR position from NAV until it clears.
    # A conflict (error 326) should fall through to clientId+1, not just give up.
    attempts = []

    def on_connect(ib, client_id):
        attempts.append(client_id)
        if client_id == ibkr_module.settings.ibkr_client_id:
            ib.errorEvent.emit(-1, ibkr_module.CLIENT_ID_IN_USE_ERROR_CODE, "already in use", None)
            raise TimeoutError()
        # fallback id succeeds -- connect() just returns normally

    monkeypatch.setattr(ibkr_module, "IB", lambda: _FakeIB(on_connect))

    ib = ibkr_module._connect()

    assert ib is not None
    assert attempts == [ibkr_module.settings.ibkr_client_id, ibkr_module.settings.ibkr_client_id + 1]


def test_connect_does_not_retry_when_genuinely_unreachable(monkeypatch):
    # A conflict and "Gateway isn't running at all" both surface as the same generic
    # TimeoutError from connect() -- only the errorEvent callback tells them apart. When
    # no conflict fires, retrying with a different clientId can't help and would just
    # waste CONNECT_TIMEOUT seconds per wasted attempt.
    attempts = []

    def on_connect(ib, client_id):
        attempts.append(client_id)
        raise TimeoutError()

    monkeypatch.setattr(ibkr_module, "IB", lambda: _FakeIB(on_connect))

    ib = ibkr_module._connect()

    assert ib is None
    assert attempts == [ibkr_module.settings.ibkr_client_id]


def test_connect_gives_up_after_exhausting_fallback_attempts(monkeypatch):
    attempts = []

    def on_connect(ib, client_id):
        attempts.append(client_id)
        ib.errorEvent.emit(-1, ibkr_module.CLIENT_ID_IN_USE_ERROR_CODE, "already in use", None)
        raise TimeoutError()

    monkeypatch.setattr(ibkr_module, "IB", lambda: _FakeIB(on_connect))

    ib = ibkr_module._connect()

    assert ib is None
    assert len(attempts) == ibkr_module.CLIENT_ID_FALLBACK_ATTEMPTS + 1
    assert attempts == list(range(ibkr_module.settings.ibkr_client_id, ibkr_module.settings.ibkr_client_id + len(attempts)))
