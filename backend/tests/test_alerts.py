from datetime import datetime, timedelta, timezone

from app import alerts


def _no_history(alert_type, symbol):
    return None


def _sent_recently(alert_type, symbol):
    return datetime.now(timezone.utc) - timedelta(hours=1)


def _sent_long_ago(alert_type, symbol):
    return datetime.now(timezone.utc) - timedelta(hours=alerts.settings.alert_cooldown_hours + 1)


def test_price_move_included_when_never_alerted(monkeypatch):
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _no_history)
    sent_calls = []
    monkeypatch.setattr(alerts, "send_email", lambda subject, html, text: sent_calls.append((subject, html, text)) or True)
    recorded = []
    monkeypatch.setattr(alerts, "record_alert_sent", lambda alert_type, symbol: recorded.append((alert_type, symbol)))

    result = alerts.check_and_send_alerts(
        price_moves=[{"symbol": "AAPL", "move": -0.092, "current_price": 190.0}],
        concentration_flags=[],
        names={},
    )

    assert result["sent"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["alert_type"] == "price_move"
    assert "1 alert" in sent_calls[0][0]  # subject is a count, not per-symbol detail
    assert "AAPL" in sent_calls[0][2]
    assert recorded == [("price_move", "AAPL")]


def test_condition_within_cooldown_is_suppressed(monkeypatch):
    # The actual bug this fixes: a price move that stays past threshold must not re-fire
    # a new email every single check interval -- only send_email being called at all
    # proves it.
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _sent_recently)
    monkeypatch.setattr(alerts, "send_email", lambda *a: (_ for _ in ()).throw(AssertionError("must not send")))
    monkeypatch.setattr(alerts, "record_alert_sent", lambda *a: (_ for _ in ()).throw(AssertionError("must not record")))

    result = alerts.check_and_send_alerts(
        price_moves=[{"symbol": "AAPL", "move": -0.092, "current_price": 190.0}],
        concentration_flags=[],
        names={},
    )

    assert result == {"sent": False, "items": []}


def test_condition_re_included_after_cooldown_lapses(monkeypatch):
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _sent_long_ago)
    monkeypatch.setattr(alerts, "send_email", lambda *a: True)
    recorded = []
    monkeypatch.setattr(alerts, "record_alert_sent", lambda alert_type, symbol: recorded.append((alert_type, symbol)))

    result = alerts.check_and_send_alerts(
        price_moves=[{"symbol": "AAPL", "move": -0.092, "current_price": 190.0}],
        concentration_flags=[],
        names={},
    )

    assert result["sent"] is True
    assert recorded == [("price_move", "AAPL")]


def test_multiple_conditions_batch_into_a_single_email(monkeypatch):
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _no_history)
    sent_calls = []
    monkeypatch.setattr(alerts, "send_email", lambda subject, html, text: sent_calls.append((subject, html, text)) or True)
    monkeypatch.setattr(alerts, "record_alert_sent", lambda *a: None)

    result = alerts.check_and_send_alerts(
        price_moves=[
            {"symbol": "AAPL", "move": -0.092, "current_price": 190.0},
            {"symbol": "TSLA", "move": -0.15, "current_price": 300.0},
        ],
        concentration_flags=[{"symbol": "IWDA", "weight": 0.51}],
        names={"IWDA": "iShares Core MSCI World UCITS ETF"},
    )

    assert len(result["items"]) == 3
    assert len(sent_calls) == 1  # one email, not three
    assert "AAPL" in sent_calls[0][2]
    assert "TSLA" in sent_calls[0][2]
    assert "IWDA" in sent_calls[0][2]
    assert "iShares Core MSCI World UCITS ETF" in sent_calls[0][2]


def test_nothing_to_alert_sends_no_email(monkeypatch):
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _no_history)
    monkeypatch.setattr(alerts, "send_email", lambda *a: (_ for _ in ()).throw(AssertionError("must not send")))
    monkeypatch.setattr(alerts, "record_alert_sent", lambda *a: (_ for _ in ()).throw(AssertionError("must not record")))

    result = alerts.check_and_send_alerts(price_moves=[], concentration_flags=[], names={})

    assert result == {"sent": False, "items": []}


def test_failed_send_does_not_record_alert_log(monkeypatch):
    # A failed/skipped send (email not configured, SMTP error) must not silently mark a
    # real unresolved condition as already-alerted -- it never actually reached the inbox.
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _no_history)
    monkeypatch.setattr(alerts, "send_email", lambda *a: False)
    monkeypatch.setattr(alerts, "record_alert_sent", lambda *a: (_ for _ in ()).throw(AssertionError("must not record")))

    result = alerts.check_and_send_alerts(
        price_moves=[{"symbol": "AAPL", "move": -0.092, "current_price": 190.0}],
        concentration_flags=[],
        names={},
    )

    assert result["sent"] is False
    assert len(result["items"]) == 1  # the condition was still detected, just not delivered


def test_price_move_and_concentration_line_formatting(monkeypatch):
    monkeypatch.setattr(alerts, "get_last_alert_sent_at", _no_history)
    sent_calls = []
    monkeypatch.setattr(alerts, "send_email", lambda subject, html, text: sent_calls.append(text) or True)
    monkeypatch.setattr(alerts, "record_alert_sent", lambda *a: None)

    alerts.check_and_send_alerts(
        price_moves=[{"symbol": "TSLA", "move": -0.15, "current_price": 300.0}],
        concentration_flags=[{"symbol": "IWDA", "weight": 0.51}],
        names={"IWDA": "iShares Core MSCI World UCITS ETF"},
    )

    text = sent_calls[0]
    assert "-15.0%" in text
    assert "300.00" in text
    assert "51%" in text
