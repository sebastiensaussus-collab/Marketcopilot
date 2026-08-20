from datetime import datetime, timedelta, timezone

from app import main


class _SyncThread:
    """Runs the target synchronously instead of spawning a real OS thread -- makes the
    catch-up tests below deterministic instead of racing a background thread."""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _set_all_report_times(monkeypatch, time_str):
    monkeypatch.setattr(main.settings, "morning_report_time", time_str)
    monkeypatch.setattr(main.settings, "lunch_report_time", time_str)
    monkeypatch.setattr(main.settings, "evening_report_time", time_str)


def test_catch_up_skips_reports_not_due_yet_today(monkeypatch):
    calls = []
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main, "_scheduled_report", lambda kind: calls.append(kind))
    _set_all_report_times(monkeypatch, "23:59")  # not due yet on any normal test run
    monkeypatch.setattr(main, "get_last_report_sent_at", lambda kind: None)

    main._catch_up_missed_reports()

    assert calls == []


def test_catch_up_skips_reports_already_sent_today(monkeypatch):
    calls = []
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main, "_scheduled_report", lambda kind: calls.append(kind))
    _set_all_report_times(monkeypatch, "00:00")  # due for the entire test run
    monkeypatch.setattr(main, "get_last_report_sent_at", lambda kind: datetime.now(timezone.utc))

    main._catch_up_missed_reports()

    assert calls == []


def test_catch_up_fires_only_the_single_most_recent_missing_report(monkeypatch):
    # The actual bug this fixes: a laptop asleep past even the generous misfire grace
    # window drops a scheduled report silently. On the next startup, this must notice and
    # send it rather than silently waiting for tomorrow's cron fire.
    #
    # Live-verified regression in an earlier version of this fix: with all three kinds
    # overdue and missing, it fired ALL of them at once -- a "Morning Brief" arriving at
    # 10pm, a real paid Claude refresh for an already-stale report, and concurrent
    # portfolio/IBKR lookups colliding with each other. Only the single freshest
    # (most-recently-scheduled) missing kind should ever be caught up.
    calls = []
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main, "_scheduled_report", lambda kind: calls.append(kind))
    monkeypatch.setattr(main.settings, "morning_report_time", "00:01")  # due, but not the latest
    monkeypatch.setattr(main.settings, "lunch_report_time", "00:02")  # due, but not the latest
    monkeypatch.setattr(main.settings, "evening_report_time", "00:03")  # the latest of the three
    monkeypatch.setattr(main, "get_last_report_sent_at", lambda kind: None)

    main._catch_up_missed_reports()

    assert calls == ["evening"]


def test_catch_up_fires_when_last_sent_was_a_previous_day(monkeypatch):
    calls = []
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main, "_scheduled_report", lambda kind: calls.append(kind))
    _set_all_report_times(monkeypatch, "00:00")
    monkeypatch.setattr(main, "get_last_report_sent_at", lambda kind: datetime.now(timezone.utc) - timedelta(days=1))

    main._catch_up_missed_reports()

    assert calls == ["morning"]  # all tied at the same time -- first in _report_kinds() order wins


def test_scheduled_report_sends_failure_notice_when_report_generation_crashes(monkeypatch):
    # Every expected failure mode (bad ticker, bad funding page, etc.) already degrades
    # gracefully inside run_full_refresh(). This covers the leftover case: something
    # genuinely unforeseen blows up send_report itself. Silence at 7:30am with no
    # indication the scheduler even ran is the one outcome worse than a crash -- there
    # must be a best-effort fallback email saying so.
    def boom(kind):
        raise RuntimeError("synthetic failure")

    sent = {}

    def fake_send_email(subject, html_body, text_body):
        sent["subject"] = subject
        sent["text_body"] = text_body
        return True

    monkeypatch.setattr(main.reports, "send_report", boom)
    monkeypatch.setattr(main.notify, "send_email", fake_send_email)

    main._scheduled_report("morning")

    assert "FAILED" in sent["subject"]
    assert "synthetic failure" in sent["text_body"]


def test_scheduled_report_does_not_raise_even_if_failure_notice_also_fails(monkeypatch):
    def boom(kind):
        raise RuntimeError("synthetic failure")

    def also_boom(subject, html_body, text_body):
        raise RuntimeError("smtp is also down")

    monkeypatch.setattr(main.reports, "send_report", boom)
    monkeypatch.setattr(main.notify, "send_email", also_boom)

    main._scheduled_report("morning")  # must not raise -- this runs inside apscheduler


def test_run_alert_check_does_not_raise_on_failure(monkeypatch):
    def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(main.alerts, "check_and_send_alerts", boom)

    main._run_alert_check()  # must not raise -- this runs inside apscheduler


def test_run_alert_check_does_not_email_on_failure(monkeypatch):
    # Deliberately different from _scheduled_report's failure-notice behavior -- this
    # runs every alert_check_interval_minutes, so emailing about every transient failure
    # would itself become the noise problem urgent alerts exist to avoid.
    monkeypatch.setattr(main.alerts, "check_and_send_alerts", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(main.notify, "send_email", lambda *a: (_ for _ in ()).throw(AssertionError("must not email")))

    main._run_alert_check()
