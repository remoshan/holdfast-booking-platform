"""The jobs the worker runs: releasing holds that ran out, and sending reminders. Each test runs
jobs as the worker does, with the clock set to when they fall due."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient, Response
from sqlalchemy import select

from holdfast.core.mail import Mailer
from holdfast.domain.jobs import FIRST_RETRY_DELAY, MAX_ATTEMPTS, JobKind, JobStatus
from holdfast.domain.reminders import REMINDER_LEAD
from holdfast.repositories import jobs
from holdfast.repositories.records import JobRecord
from holdfast.services.jobs import run_next_job

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Inbox = Callable[[], Awaitable[list[Json]]]


async def test_a_hold_that_runs_out_expires_and_frees_its_slot(
    app: FastAPI, client: AsyncClient, organisation: Json, book: Book, mailer: Mailer
) -> None:
    held = (await book("09:00", "10:00")).json()

    ran = await run_next_job(
        app.state.sessions, mailer, datetime.fromisoformat(held["hold_expires_at"])
    )

    booking = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    assert ran
    assert booking.json()["status"] == "expired"
    assert (await book("09:00", "10:00")).status_code == status.HTTP_201_CREATED


async def test_a_job_waits_until_it_is_due(
    app: FastAPI, client: AsyncClient, organisation: Json, book: Book, mailer: Mailer
) -> None:
    held = (await book("09:00", "10:00")).json()
    just_before = datetime.fromisoformat(held["hold_expires_at"]) - timedelta(microseconds=1)

    ran = await run_next_job(app.state.sessions, mailer, just_before)

    booking = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    assert not ran
    assert booking.json()["status"] == "held"


async def test_a_hold_confirmed_in_time_does_not_expire(
    app: FastAPI, client: AsyncClient, organisation: Json, book: Book, mailer: Mailer
) -> None:
    held = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{held['id']}"
    await client.post(f"{path}/confirm")

    await run_next_job(app.state.sessions, mailer, datetime.fromisoformat(held["hold_expires_at"]))

    assert (await client.get(path)).json()["status"] == "confirmed"
    assert [job.kind for job in await _jobs(app)] == [JobKind.SEND_REMINDER]


async def test_a_confirmed_booking_is_reminded_by_email_a_day_before_it_starts(
    app: FastAPI, book: Book, free_resource: Json, user: Json, mailer: Mailer, inbox: Inbox
) -> None:
    booking = (await book("09:00", "10:30", resource_id=free_resource["id"])).json()
    remind_at = datetime.fromisoformat(booking["start"]) - REMINDER_LEAD
    [reminder] = await _jobs(app)

    ran = await run_next_job(app.state.sessions, mailer, remind_at)

    [email] = await inbox()
    assert ran
    assert (reminder.kind, reminder.run_at) == (JobKind.SEND_REMINDER, remind_at)
    assert [recipient["Address"] for recipient in email["To"]] == [user["email"]]
    assert email["Subject"].startswith("Reminder: Desk 1, ")
    assert email["MessageID"] == f"reminder.{booking['id']}@holdfast"
    assert await _jobs(app) == []


async def test_a_cancelled_booking_gets_no_reminder(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    free_resource: Json,
    mailer: Mailer,
    inbox: Inbox,
) -> None:
    booking = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    await client.post(f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel")

    ran = await run_next_job(
        app.state.sessions, mailer, datetime.fromisoformat(booking["start"]) - REMINDER_LEAD
    )

    assert ran
    assert await inbox() == []
    assert await _jobs(app) == []


async def test_a_failing_job_is_retried_later_until_its_last_attempt_leaves_it_dead(
    app: FastAPI, mailer: Mailer
) -> None:
    """A job for a booking that does not exist fails every time."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    async with app.state.sessions() as session, session.begin():
        payload = {"organisation_id": str(uuid4()), "booking_id": str(booking_id)}
        await jobs.enqueue(session, JobKind.EXPIRE_HOLD, payload, now)

    await run_next_job(app.state.sessions, mailer, now)
    [first_failure] = await _jobs(app)
    for _ in range(MAX_ATTEMPTS - 1):
        [job] = await _jobs(app)
        await run_next_job(app.state.sessions, mailer, job.run_at)
    [dead] = await _jobs(app)

    assert (first_failure.status, first_failure.attempts) == (JobStatus.QUEUED, 1)
    assert first_failure.run_at == now + FIRST_RETRY_DELAY
    assert first_failure.last_error == f"NotFoundError: Booking {booking_id} was not found."
    assert (dead.status, dead.attempts) == (JobStatus.DEAD, MAX_ATTEMPTS)
    assert not await run_next_job(app.state.sessions, mailer, dead.run_at + timedelta(days=365))


async def _jobs(app: FastAPI) -> list[JobRecord]:
    async with app.state.sessions() as session:
        return list(await session.scalars(select(JobRecord).order_by(JobRecord.run_at)))
