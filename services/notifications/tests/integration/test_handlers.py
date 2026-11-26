"""What the service does with each event it receives: the email it sends, or why it sends none.
Each test stores the event as the worker's consumer does, then runs its job as the worker would."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import receive
from holdfast.messaging.jobs import run_next_job
from holdfast.messaging.records import JobRecord
from holdfast.notifications.handlers import handlers
from holdfast.notifications.mail import Mailer

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Inbox = Callable[[], Awaitable[list[Json]]]
type Sessions = async_sessionmaker[AsyncSession]
type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]


@pytest.mark.parametrize(
    ("message_type", "extra_data", "subject", "kind"),
    [
        (
            "bookings.booking_confirmed",
            {"price": 2000, "currency": "EUR"},
            "Booking confirmed",
            "confirmed",
        ),
        ("bookings.booking_cancelled", {}, "Booking cancelled", "cancelled"),
        ("bookings.reminder_due", {}, "Reminder", "reminder"),
        (
            "bookings.payment_failed",
            {"price": 2000, "currency": "EUR"},
            "Payment failed",
            "payment_failed",
        ),
        (
            "bookings.refund_issued",
            {"amount": 2000, "currency": "EUR"},
            "Refund issued",
            "refund_issued",
        ),
    ],
)
async def test_each_event_is_emailed_to_the_booker(
    sessions: Sessions,
    mailer: Mailer,
    inbox: Inbox,
    stored_jobs: StoredJobs,
    message_type: str,
    extra_data: Json,
    subject: str,
    kind: str,
) -> None:
    now = datetime.now(UTC)
    booking = _booking(start=now + timedelta(days=1)) | extra_data
    await _receive(sessions, message_type, booking, now)

    ran = await run_next_job(sessions, handlers(mailer), now)

    [email] = await inbox()
    assert ran
    assert [recipient["Address"] for recipient in email["To"]] == [booking["recipient"]]
    assert email["Subject"].startswith(f"{subject}: Room 1, ")
    assert email["MessageID"] == f"{kind}.{booking['booking_id']}@holdfast"
    assert await stored_jobs() == []


async def test_a_reminder_that_arrives_once_the_booking_has_started_is_dropped(
    sessions: Sessions, mailer: Mailer, inbox: Inbox, stored_jobs: StoredJobs
) -> None:
    """Held up by an outage, it would be of no use any more."""
    now = datetime.now(UTC)
    await _receive(sessions, "bookings.reminder_due", _booking(start=now), now)

    ran = await run_next_job(sessions, handlers(mailer), now)

    assert ran
    assert await inbox() == []
    assert await stored_jobs() == []


def _booking(start: datetime) -> Json:
    """What every event about a booking carries, for one that starts at `start`."""
    return {
        "booking_id": str(uuid4()),
        "recipient": "ada@example.com",
        "resource_name": "Room 1",
        "location_name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": "Europe/Amsterdam",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
    }


async def _receive(sessions: Sessions, message_type: str, data: Json, now: datetime) -> None:
    envelope = Envelope(id=uuid4(), type=message_type, time=now, data=data)
    async with sessions() as session, session.begin():
        await receive(session, envelope.model_dump_json().encode(), now)
