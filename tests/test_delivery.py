from __future__ import annotations

import smtplib
from typing import Any, ClassVar, Self

import pytest

from digest.config import Config
from digest.delivery.smtp import SmtpDeliverer
from digest.errors import DeliveryError


class _FakeSmtp:
    """Stands in for smtplib.SMTP — no test may touch the network (CLAUDE.md 7)."""

    instances: ClassVar[list[_FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: Any = None
        _FakeSmtp.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: Any) -> None:
        self.sent = message


@pytest.fixture(autouse=True)
def _reset_fake_smtp() -> None:
    _FakeSmtp.instances.clear()


def test_send_raises_without_smtp_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    config = Config(recipient_email="me@example.com")

    with pytest.raises(DeliveryError):
        SmtpDeliverer().send("subject", "<p>hi</p>", "hi", config)


def test_send_is_a_no_op_without_a_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    config = Config(recipient_email=None)

    # AUDIT-2 BLOCKER: cli._deliver's record_sent gate depends on this return value —
    # a graceful skip must report False, not just avoid raising.
    assert SmtpDeliverer().send("subject", "<p>hi</p>", "hi", config) is False
    assert _FakeSmtp.instances == []


def test_send_builds_a_multipart_alternative_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    config = Config(recipient_email="you@example.com")

    assert SmtpDeliverer().send("Tárgy", "<p>hí</p>", "hí", config) is True

    (smtp,) = _FakeSmtp.instances
    assert smtp.host == "smtp.example.com"
    assert smtp.started_tls is True
    assert smtp.login_args == ("me@example.com", "app-password")
    message = smtp.sent
    assert message["Subject"] == "Tárgy"
    assert message["To"] == "you@example.com"
    assert message.is_multipart()
    payloads = {part.get_content_type(): part.get_content() for part in message.iter_parts()}
    assert payloads["text/plain"].strip() == "hí"
    assert "<p>hí</p>" in payloads["text/html"]


def test_type_is_smtp() -> None:
    assert SmtpDeliverer().type == "smtp"
