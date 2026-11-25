"""The booking service's jobs, which release holds that ran out and send reminders, and its worker,
which also deletes ended sign-ins. Most tests run jobs as the worker does, with the clock set to
when they fall due. The job queue itself is tested with the messaging library."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from holdfast.bookings import worker
from holdfast.bookings.core.config import WorkerSettings
from holdfast.bookings.core.mail import Mailer
from holdfast.bookings.domain.reminders import REMINDER_LEAD
from holdfast.bookings.repositories import bookings
from holdfast.bookings.repositories.records import RefreshTokenRecord
from holdfast.bookings.services import jobs as job_service
from holdfast.bookings.services.jobs import JobKind
from holdfast.messaging.jobs import run_next_job
from holdfast.messaging.records import JobRecord

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Inbox = Callable[[], Awaitable[list[Json]]]
type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]
type Wait = Callable[[], Awaitable[None]]

WAIT_SECONDS = 10
"""How long a test waits for something that should happen at once."""


@pytest.fixture
def worker_settings(database_url: str, mailer: Mailer) -> WorkerSettings:
    """Nothing listens at the broker's address: the worker's jobs and chores need none."""
    return WorkerSettings(
        database_url=database_url,
        rabbitmq_url="amqp://holdfast:holdfast@127.0.0.1:1/",
        smtp_host=mailer.host,
        smtp_port=mailer.port,
        mail_from=mailer.sender,
    )


async def test_a_hold_that_runs_out_expires_and_frees_its_slot(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    resource: Json,
    book: Book,
    mailer: Mailer,
    booking_day: date,
    at: Callable[..., str],
) -> None:
    held = (await book("09:00", "10:00")).json()

    ran = await run_next_job(
        app.state.sessions,
        job_service.handlers(mailer),
        datetime.fromisoformat(held["hold_expires_at"]),
    )

    booking = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    availability = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/availability",
        params={"date": booking_day.isoformat()},
    )
    upcoming = await client.get("/users/me/bookings")
    assert ran
    assert booking.json()["status"] == "expired"
    assert [
        (datetime.fromisoformat(period["start"]), datetime.fromisoformat(period["end"]))
        for period in availability.json()["free"]
    ] == [(datetime.fromisoformat(at("08:00")), datetime.fromisoformat(at("18:00")))]
    assert upcoming.json()["items"] == []
    assert (await book("09:00", "10:00")).status_code == status.HTTP_201_CREATED


async def test_a_job_waits_until_it_is_due(
    app: FastAPI, client: AsyncClient, organisation: Json, book: Book, mailer: Mailer
) -> None:
    held = (await book("09:00", "10:00")).json()
    just_before = datetime.fromisoformat(held["hold_expires_at"]) - timedelta(microseconds=1)

    ran = await run_next_job(app.state.sessions, job_service.handlers(mailer), just_before)

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

    await run_next_job(
        app.state.sessions,
        job_service.handlers(mailer),
        datetime.fromisoformat(held["hold_expires_at"]),
    )

    assert (await client.get(path)).json()["status"] == "confirmed"
    assert [job.kind for job in await stored_jobs()] == [JobKind.SEND_REMINDER]


async def test_a_hold_confirmed_while_its_expiry_job_runs_stays_confirmed(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    mailer: Mailer,
    stored_jobs: StoredJobs,
    until_a_query_waits_for_a_lock: Wait,
) -> None:
    """Confirming locks the booking's row, so the expiry job waits for the confirmation to commit
    and then sees it. Without its own lock, the job would expire the booking over it."""
    held = (await book("09:00", "10:00")).json()
    hold_expires_at = datetime.fromisoformat(held["hold_expires_at"])
    async with app.state.sessions() as confirming, confirming.begin():
        booking = await bookings.get(
            confirming, UUID(organisation["id"]), UUID(held["id"]), for_update=True
        )
        expiring = asyncio.create_task(
            run_next_job(app.state.sessions, job_service.handlers(mailer), hold_expires_at)
        )
        await until_a_query_waits_for_a_lock()
        just_in_time = hold_expires_at - timedelta(seconds=1)
        await bookings.save_status(confirming, booking.confirm(just_in_time))
    ran = await expiring

    response = await client.get(f"/organisations/{organisation['id']}/bookings/{held['id']}")
    assert ran
    assert response.json()["status"] == "confirmed"
    assert await stored_jobs() == []


@pytest.mark.parametrize(("action", "status_code"), [("confirm", 409), ("cancel", 200)])
async def test_confirming_or_cancelling_while_a_hold_expires_waits_and_finds_it_expired(
    app: FastAPI,
    client: AsyncClient,
    organisation: Json,
    book: Book,
    until_a_query_waits_for_a_lock: Wait,
    action: str,
    status_code: int,
) -> None:
    """Confirming and cancelling lock the booking's row, as the expiry job does. Without that
    lock, a request that read the hold before the expiry committed would write over it, after
    the slot had been freed."""
    held = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{held['id']}"
    async with app.state.sessions() as expiring, expiring.begin():
        booking = await bookings.get(
            expiring, UUID(organisation["id"]), UUID(held["id"]), for_update=True
        )
        hold_expires_at = datetime.fromisoformat(held["hold_expires_at"])
        await bookings.save_status(expiring, booking.expire(hold_expires_at))
        request = asyncio.create_task(client.post(f"{path}/{action}"))
        await until_a_query_waits_for_a_lock()
    response = await request

    assert response.status_code == status_code
    assert (await client.get(path)).json()["status"] == "expired"


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

    ran = await run_next_job(app.state.sessions, job_service.handlers(mailer), remind_at)

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
        app.state.sessions,
        job_service.handlers(mailer),
        datetime.fromisoformat(booking["start"]) - REMINDER_LEAD,
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
        app.state.sessions,
        job_service.handlers(mailer),
        datetime.fromisoformat(booking["start"]) - REMINDER_LEAD,
    )

    assert ran
    assert await inbox() == []
    assert await stored_jobs() == []


async def test_the_worker_deletes_ended_sign_ins(
    app: FastAPI, anonymous_client: AsyncClient, user: Json, worker_settings: WorkerSettings
) -> None:
    """It does so as soon as it starts."""
    await anonymous_client.post("/auth/logout", json={"refresh_token": user["refresh_token"]})
    assert await _count_refresh_tokens(app) == 1

    async def purged() -> bool:
        return await _count_refresh_tokens(app) == 0

    await _run_worker_until(worker_settings, purged)

    assert await _count_refresh_tokens(app) == 0


async def _run_worker_until(settings: WorkerSettings, done: Callable[[], Awaitable[bool]]) -> None:
    """Run the worker until `done` returns true, then stop it as a signal would."""
    stopping = asyncio.Event()
    running = asyncio.create_task(worker.run_worker(settings, stopping))
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                if await done():
                    break
                await asyncio.sleep(0.05)
    finally:
        stopping.set()
        await running


async def _count_refresh_tokens(app: FastAPI) -> int:
    async with app.state.sessions() as session:
        return await session.scalar(select(func.count()).select_from(RefreshTokenRecord)) or 0
