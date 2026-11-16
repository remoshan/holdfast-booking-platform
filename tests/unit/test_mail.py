"""The email the worker sends: its sender, its date, and how long sending it may take."""

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Self

import pytest

from holdfast.core.config import WorkerSettings
from holdfast.core.mail import SMTP_TIMEOUT_SECONDS, Mailer
from holdfast.worker import IDLE_IN_TRANSACTION_TIMEOUT


@pytest.mark.parametrize(
    ("mail_from", "address"),
    [
        ('"Acme, Inc." <reminders@acme.example>', ("Acme, Inc.", "reminders@acme.example")),
        ("Holdfast <reminders@b\u00fccher.example>", ("Holdfast", "reminders@b\u00fccher.example")),
    ],
    ids=["a comma in the name", "an internationalised domain"],
)
def test_the_sender_is_one_address(mail_from: str, address: tuple[str, str]) -> None:
    settings = WorkerSettings(
        database_url="postgresql+asyncpg://holdfast:holdfast@localhost/holdfast",
        smtp_host="localhost",
        smtp_port=25,
        mail_from=mail_from,
    )

    assert getaddresses([settings.sender]) == [address]


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


def test_the_workers_session_limit_outlasts_the_slowest_send() -> None:
    """A send waits for a connection and eight replies, each for up to the SMTP timeout. If
    Postgres ended the worker's idle session sooner, the attempt would go unrecorded and the
    reminder would be sent again and again."""
    session_limit_seconds = int(IDLE_IN_TRANSACTION_TIMEOUT.removesuffix("s"))

    assert session_limit_seconds > 2 * 9 * SMTP_TIMEOUT_SECONDS
