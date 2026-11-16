"""The jobs the worker runs: releasing holds that ran out, and sending reminders. Each test runs
jobs as the worker does, with the clock set to when they fall due."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.core.mail import Mailer
from holdfast.domain.jobs import FIRST_RETRY_DELAY, MAX_ATTEMPTS, JobKind, JobStatus
from holdfast.domain.reminders import REMINDER_LEAD
from holdfast.repositories import bookings, jobs
from holdfast.repositories.records import JobRecord
from holdfast.services import jobs as job_service
from holdfast.services.jobs import run_next_job

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Inbox = Callable[[], Awaitable[list[Json]]]
type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]

LOCK_WAIT_SECONDS = 10


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
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    mailer: Mailer,
    stored_jobs: StoredJobs,
) -> None:
    held = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{held['id']}"
    await client.post(f"{path}/confirm")

    await run_next_job(app.state.sessions, mailer, datetime.fromisoformat(held["hold_expires_at"]))

    assert (await client.get(path)).json()["status"] == "confirmed"
    assert [job.kind for job in await stored_jobs()] == [JobKind.SEND_REMINDER]


async def test_a_hold_confirmed_while_its_expiry_job_runs_stays_confirmed(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    mailer: Mailer,
    stored_jobs: StoredJobs,
) -> None:
    """Confirming locks the booking's row, so the expiry job waits for the confirmation to commit
    and then sees it. Without its own lock, the job would expire the booking over it."""
    held = (await book("09:00", "10:00")).json()
    hold_expires_at = datetime.fromisoformat(held["hold_expires_at"])
    async with app.state.sessions() as confirming, confirming.begin():
        booking = await bookings.get(
            confirming, UUID(organisation["id"]), UUID(held["id"]), for_update=True
        )
        expiring = asyncio.create_task(run_next_job(app.state.sessions, mailer, hold_expires_at))
        await _until_a_query_waits_for_a_lock(app)
        just_in_time = hold_expires_at - timedelta(seconds=1)
        await bookings.save_status(confirming, booking.confirm(just_in_time))
    ran = await expiring

    response = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    assert ran
    assert response.json()["status"] == "confirmed"
    assert await stored_jobs() == []


async def test_a_confirmed_booking_is_reminded_by_email_a_day_before_it_starts(
    app: FastAPI,
    book: Book,
    free_resource: Json,
    user: Json,
    mailer: Mailer,
    inbox: Inbox,
    stored_jobs: StoredJobs,
) -> None:
    booking = (await book("09:00", "10:30", resource_id=free_resource["id"])).json()
    remind_at = datetime.fromisoformat(booking["start"]) - REMINDER_LEAD
    [reminder] = await stored_jobs()

    ran = await run_next_job(app.state.sessions, mailer, remind_at)

    [email] = await inbox()
    assert ran
    assert (reminder.kind, reminder.run_at) == (JobKind.SEND_REMINDER, remind_at)
    assert [recipient["Address"] for recipient in email["To"]] == [user["email"]]
    assert email["Subject"].startswith("Reminder: Desk 1, ")
    assert email["MessageID"] == f"reminder.{booking['id']}@holdfast"
    assert await stored_jobs() == []


async def test_a_cancelled_booking_gets_no_reminder(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    free_resource: Json,
    mailer: Mailer,
    inbox: Inbox,
    stored_jobs: StoredJobs,
) -> None:
    booking = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    await client.post(f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel")

    ran = await run_next_job(
        app.state.sessions, mailer, datetime.fromisoformat(booking["start"]) - REMINDER_LEAD
    )

    assert ran
    assert await inbox() == []
    assert await stored_jobs() == []


async def test_a_booker_who_has_left_the_organisation_gets_no_reminder(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    free_resource: Json,
    add_member: Callable[..., Awaitable[Json]],
    mailer: Mailer,
    inbox: Inbox,
    stored_jobs: StoredJobs,
) -> None:
    member = await add_member(organisation["id"], "member")
    booking = (
        await book("09:00", "10:00", headers=member["headers"], resource_id=free_resource["id"])
    ).json()
    await client.delete(f"/organisations/{organisation['id']}/members/{member['id']}")

    ran = await run_next_job(
        app.state.sessions, mailer, datetime.fromisoformat(booking["start"]) - REMINDER_LEAD
    )

    assert ran
    assert await inbox() == []
    assert await stored_jobs() == []


async def test_a_failing_job_is_retried_later_until_its_last_attempt_leaves_it_dead(
    app: FastAPI, mailer: Mailer, stored_jobs: StoredJobs
) -> None:
    """A job for a booking that does not exist fails every time."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    async with app.state.sessions() as session, session.begin():
        payload = {"organisation_id": str(uuid4()), "booking_id": str(booking_id)}
        await jobs.enqueue(session, JobKind.EXPIRE_HOLD, payload, now)

    await run_next_job(app.state.sessions, mailer, now)
    [first_failure] = await stored_jobs()
    for _ in range(MAX_ATTEMPTS - 1):
        [job] = await stored_jobs()
        await run_next_job(app.state.sessions, mailer, job.run_at)
    [dead] = await stored_jobs()

    assert (first_failure.status, first_failure.attempts) == (JobStatus.QUEUED, 1)
    assert first_failure.run_at == now + FIRST_RETRY_DELAY
    assert first_failure.last_error == f"NotFoundError: Booking {booking_id} was not found."
    assert (dead.status, dead.attempts) == (JobStatus.DEAD, MAX_ATTEMPTS)
    assert not await run_next_job(app.state.sessions, mailer, dead.run_at + timedelta(days=365))


async def test_a_job_that_fails_at_the_database_leaves_only_its_failure_behind(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    mailer: Mailer,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The savepoint undoes the job's own writes and keeps the transaction usable after the
    database refused a statement, so the failure can still be recorded."""

    async def expire_then_fail(
        session: AsyncSession, organisation_id: UUID, booking_id: UUID, now: datetime
    ) -> None:
        booking = await bookings.get(session, organisation_id, booking_id, for_update=True)
        await bookings.save_status(session, booking.expire(now))
        await session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(job_service, "_expire_hold", expire_then_fail)
    held = (await book("09:00", "10:00")).json()
    hold_expires_at = datetime.fromisoformat(held["hold_expires_at"])

    ran = await run_next_job(app.state.sessions, mailer, hold_expires_at)

    [job] = await stored_jobs()
    booking = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    assert ran
    assert booking.json()["status"] == "held"
    assert (job.attempts, job.run_at) == (1, hold_expires_at + FIRST_RETRY_DELAY)
    assert "division by zero" in (job.last_error or "")


async def _until_a_query_waits_for_a_lock(app: FastAPI) -> None:
    async with app.state.sessions() as session, asyncio.timeout(LOCK_WAIT_SECONDS):
        while True:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND backend_type = 'client backend' AND wait_event_type = 'Lock'"
                )
            )
            if waiting:
                return
            await session.rollback()  # the next count takes a fresh snapshot
            await asyncio.sleep(0.05)
