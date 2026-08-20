from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import structlog

from digest.config import Config
from digest.errors import DeliveryError

log = structlog.get_logger()

_DEFAULT_PORT = 587
_TIMEOUT_SECONDS = 30


class SmtpDeliverer:
    """SPEC 10: Gmail app password via SMTP_HOST/SMTP_USER/SMTP_PASSWORD from the
    environment, recipient from the profile. Never on the critical path in a test — real
    delivery happens only when a caller actually invokes `send`."""

    type = "smtp"

    def send(self, subject: str, html: str, text: str, config: Config) -> bool:
        if not config.recipient_email:
            log.warning("smtp_skipped", reason="no recipient_email configured")
            return False

        host = os.environ.get("SMTP_HOST")
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        if not host or not user or not password:
            raise DeliveryError("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must all be set")
        port = int(os.environ.get("SMTP_PORT", _DEFAULT_PORT))

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = user
        message["To"] = config.recipient_email
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        with smtplib.SMTP(host, port, timeout=_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
        # AUDIT-3 BLOCKER: this used to log to=config.recipient_email — a PROFILE_YAML
        # secret (SPEC 12) — in plain text on every successful send, straight into the
        # public GitHub Actions log once the repo is public. subject alone is not personal
        # data (SPEC 9's generated subject line is a date and a count).
        log.info("email_sent", subject=subject)
        return True
