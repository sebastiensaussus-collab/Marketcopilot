"""Sends report emails via SMTP (Gmail App Password by default). Fails loudly in logs
but never raises into a scheduled job -- a broken email shouldn't crash the scheduler.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.settings import settings

logger = logging.getLogger("market_copilot.notify")


def is_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_app_password)


def send_email(subject: str, html_body: str, text_body: str) -> bool:
    if not is_configured():
        logger.warning("Email not configured (smtp_user/smtp_app_password unset) -- skipping send")
        return False

    recipient = settings.report_recipient or settings.smtp_user

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain"))
    message.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_app_password)
            server.sendmail(settings.smtp_user, [recipient], message.as_string())
        logger.info("Sent report email '%s' to %s", subject, recipient)
        return True
    except Exception:
        logger.exception("Failed to send report email '%s'", subject)
        return False
