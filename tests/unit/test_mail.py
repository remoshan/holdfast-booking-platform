"""The email the worker sends: who it comes from, and when it was sent."""

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Self

import pytest

from holdfast.core.config import WorkerSettings
from holdfast.core.mail import Mailer


def test_a_sender_name_with_a_comma_stays_one_address() -> None:
    settings = WorkerSettings(
        database_url="postgresql+asyncpg://holdfast:holdfast@localhost/holdfast",
        smtp_host="localhost",
        smtp_port=25,
        mail_from='"Acme, Inc." <reminders@acme.example>',
    )

    assert getaddresses([settings.sender]) == [("Acme, Inc.", "reminders@acme.example")]


def test_every_email_carries_the_date_it_was_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[EmailMessage] = []

    class CapturingSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exception: object) -> None:
            pass

        def send_message(self, message: EmailMessage) -> None:
            sent.append(message)

    monkeypatch.setattr(smtplib, "SMTP", CapturingSMTP)
    mailer = Mailer("mail.example", 25, "Holdfast <reminders@example.com>")

    asyncio.run(mailer.send("ada@example.com", "Reminder", "Body", "<reminder.1@holdfast>"))

    [message] = sent
    assert abs(parsedate_to_datetime(message["Date"]) - datetime.now(UTC)) < timedelta(minutes=1)
