"""The email the worker sends: its sender, its date, its encoding, when it counts as sent, and how
long sending it may take."""

import asyncio
import smtplib
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import pytest

from holdfast.core import mail
from holdfast.core.config import WorkerSettings
from holdfast.core.mail import SEND_DEADLINE_SECONDS, SMTP_TIMEOUT_SECONDS, Mailer
from holdfast.worker import IDLE_IN_TRANSACTION_TIMEOUT

type Conversation = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


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


def test_a_body_that_is_not_plain_ascii_is_encoded_for_any_mail_server() -> None:
    """SMTP may carry 8-bit text only to a server that offers 8BITMIME, and smtplib asks for that
    only when an address needs SMTPUTF8."""
    body = "Caf\u00e9 Z\u00fcrich, from 09:00 to 10:00"

    [accepted] = asyncio.run(_send_a_reminder(body=body))

    assert accepted.isascii()
    assert message_from_bytes(accepted, policy=policy.default).get_content().splitlines() == [body]


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


@pytest.mark.parametrize(
    "connect_delay", [0, 1.5], ids=["once connected", "while still connecting"]
)
def test_a_send_that_a_slow_server_drags_out_is_cut_off_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch, connect_delay: float
) -> None:
    """A server that keeps sending something never trips the timeout for each wait, so only the
    deadline stops it holding up the worker. This one sends its greeting a line at a time. A send
    that is still connecting when the deadline passes is cut off as soon as it connects."""
    connect = socket.create_connection

    def connect_slowly(*args: Any, **kwargs: Any) -> socket.socket:
        time.sleep(connect_delay)
        return connect(*args, **kwargs)

    monkeypatch.setattr(mail, "SEND_DEADLINE_SECONDS", 1)
    monkeypatch.setattr(socket, "create_connection", connect_slowly)
    started = time.monotonic()

    with pytest.raises(smtplib.SMTPServerDisconnected):
        asyncio.run(_send_through(_drag_out_the_greeting))

    assert time.monotonic() - started < 5


def test_the_workers_session_limit_outlasts_the_longest_send() -> None:
    """A send ends at its deadline, or on Windows once the server next sends something, within
    the SMTP timeout. If Postgres ended the worker's idle session sooner, the attempt would go
    unrecorded and the reminder would be sent again and again."""
    session_limit_seconds = int(IDLE_IN_TRANSACTION_TIMEOUT.removesuffix("s"))

    assert session_limit_seconds > SEND_DEADLINE_SECONDS + SMTP_TIMEOUT_SECONDS


async def _send_a_reminder(
    *,
    body: str = "Body",
    data_reply: bytes = b"250 Accepted\r\n",
    quit_reply: bytes = b"221 Bye\r\n",
) -> list[bytes]:
    """Send a reminder to a stand-in mail server, which answers the email with data_reply and QUIT
    with quit_reply, or hangs up in place of an empty reply. Returns the emails it received."""
    received: list[bytes] = []

    async def converse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 Ready\r\n")
        while command := (await reader.readline()).upper():
            if command.startswith(b"DATA"):
                writer.write(b"354 Go ahead\r\n")
                email = await reader.readuntil(b"\r\n.\r\n")
                received.append(email.removesuffix(b".\r\n"))
                if not data_reply:
                    break
                writer.write(data_reply)
            elif command.startswith(b"QUIT"):
                writer.write(quit_reply)
                break
            else:
                writer.write(b"250 OK\r\n")
        writer.close()

    await _send_through(converse, body)
    return received


async def _drag_out_the_greeting(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Send a line of greeting every tenth of a second, whatever the client does, until the
    connection breaks. Give up after ten seconds, so that a send with no deadline fails the test
    instead of hanging it."""
    with suppress(ConnectionError):
        for _ in range(100):
            writer.write(b"220-Still here\r\n")
            await writer.drain()
            await asyncio.sleep(0.1)
    writer.close()


async def _send_through(conversation: Conversation, body: str = "Body") -> None:
    """Send a reminder through the real smtplib to a stand-in mail server that holds the given
    conversation."""
    async with await asyncio.start_server(conversation, "127.0.0.1", 0) as server:
        host, port = server.sockets[0].getsockname()
        mailer = Mailer(host, port, "Holdfast <reminders@example.com>")
        await mailer.send("ada@example.com", "Reminder", body, "<reminder.1@holdfast>")
