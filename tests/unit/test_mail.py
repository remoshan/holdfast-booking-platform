"""The email the worker sends: its sender, its date, when it counts as sent, and how long sending
it may take."""

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.utils import getaddresses, parsedate_to_datetime

import pytest

from holdfast.core.config import WorkerSettings
from holdfast.core.mail import SMTP_TIMEOUT_SECONDS, Mailer
from holdfast.worker import IDLE_IN_TRANSACTION_TIMEOUT


@pytest.mark.parametrize(
    ("mail_from", "address"),
    [
        ('"Acme, Inc." <reminders@acme.example>', ("Acme, Inc.", "reminders@acme.example")),
        ("Holdfast <reminders@b\u00fccher.example>", ("Holdfast", "reminders@b\u00fccher.example")),
        ("Holdfast <r\u00e9minders@example.com>", ("Holdfast", "r\u00e9minders@example.com")),
    ],
    ids=["a comma in the name", "an internationalised domain", "a non-ASCII local part"],
)
def test_the_sender_is_one_address(mail_from: str, address: tuple[str, str]) -> None:
    settings = WorkerSettings(
        database_url="postgresql+asyncpg://holdfast:holdfast@localhost/holdfast",
        smtp_host="localhost",
        smtp_port=25,
        mail_from=mail_from,
    )

    assert getaddresses([settings.sender]) == [address]


def test_every_email_carries_the_date_it_was_sent() -> None:
    [accepted] = asyncio.run(_send_a_reminder())

    message = message_from_bytes(accepted)
    assert abs(parsedate_to_datetime(message["Date"]) - datetime.now(UTC)) < timedelta(minutes=1)


@pytest.mark.parametrize(
    "quit_reply", [b"554 Not now\r\n", b""], ids=["an error reply", "a hang-up"]
)
def test_an_email_the_server_accepted_counts_as_sent_whatever_follows(quit_reply: bytes) -> None:
    """Otherwise the job would fail, and its next attempt would send the email again."""
    accepted = asyncio.run(_send_a_reminder(quit_reply=quit_reply))

    assert len(accepted) == 1


@pytest.mark.parametrize("data_reply", [b"554 Rejected\r\n", b""], ids=["a refusal", "a hang-up"])
def test_an_email_the_server_did_not_accept_fails_the_attempt(data_reply: bytes) -> None:
    """So the job tries again later, instead of losing the reminder."""
    with pytest.raises(smtplib.SMTPException):
        asyncio.run(_send_a_reminder(data_reply=data_reply))


async def _send_a_reminder(
    *, data_reply: bytes = b"250 Accepted\r\n", quit_reply: bytes = b"221 Bye\r\n"
) -> list[bytes]:
    """Send a reminder through the real smtplib to a stand-in mail server, which answers the
    email with data_reply and QUIT with quit_reply, or hangs up in place of an empty reply.
    Returns the emails it received."""
    received: list[bytes] = []

    async def converse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 Ready\r\n")
        while command := (await reader.readline()).upper():
            if command.startswith(b"DATA"):
                writer.write(b"354 Go ahead\r\n")
                received.append(await reader.readuntil(b"\r\n.\r\n"))
                if not data_reply:
                    break
                writer.write(data_reply)
            elif command.startswith(b"QUIT"):
                writer.write(quit_reply)
                break
            else:
                writer.write(b"250 OK\r\n")
        writer.close()

    async with await asyncio.start_server(converse, "127.0.0.1", 0) as server:
        host, port = server.sockets[0].getsockname()
        mailer = Mailer(host, port, "Holdfast <reminders@example.com>")
        await mailer.send("ada@example.com", "Reminder", "Body", "<reminder.1@holdfast>")
    return received


def test_the_workers_session_limit_outlasts_the_slowest_send() -> None:
    """A send waits for a connection and eight replies, each for up to the SMTP timeout. If
    Postgres ended the worker's idle session sooner, the attempt would go unrecorded and the
    reminder would be sent again and again."""
    session_limit_seconds = int(IDLE_IN_TRANSACTION_TIMEOUT.removesuffix("s"))

    assert session_limit_seconds > 2 * 9 * SMTP_TIMEOUT_SECONDS
