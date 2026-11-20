"""The worker: the jobs it runs, which release holds that ran out and send reminders, and the loop
that runs them. Most tests run jobs as the loop does, with the clock set to when they fall due."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast import worker
from holdfast.core.config import WorkerSettings
from holdfast.core.mail import Mailer
from holdfast.domain.jobs import FIRST_RETRY_DELAY, MAX_ATTEMPTS, JobKind, JobStatus
from holdfast.domain.reminders import REMINDER_LEAD
from holdfast.repositories import bookings, jobs
from holdfast.repositories.records import JobRecord, RefreshTokenRecord
from holdfast.services import jobs as job_service
from holdfast.services.jobs import run_next_job

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
    return WorkerSettings(
        database_url=database_url,
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
        app.state.sessions, mailer, datetime.fromisoformat(held["hold_expires_at"])
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
        expiring = asyncio.create_task(run_next_job(app.state.sessions, mailer, hold_expires_at))
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


async def test_a_worker_passes_over_a_job_another_holds_and_runs_the_most_overdue_of_the_rest(
    app: FastAPI, mailer: Mailer, stored_jobs: StoredJobs
) -> None:
    """Each job is for a booking that does not exist, so the one that runs records a failed
    attempt. Without SKIP LOCKED, the worker would wait for the job that the other one holds."""
    now = datetime.now(UTC)
    booking_ids = {minutes_overdue: str(uuid4()) for minutes_overdue in (1, 3, 2)}
    async with app.state.sessions() as session, session.begin():
        for minutes_overdue, booking_id in booking_ids.items():
            payload = {"organisation_id": str(uuid4()), "booking_id": booking_id}
            run_at = now - timedelta(minutes=minutes_overdue)
            await jobs.enqueue(session, JobKind.EXPIRE_HOLD, payload, run_at)

    async with app.state.sessions() as other_worker, other_worker.begin():
        held = await jobs.claim_next(other_worker, now)
        async with asyncio.timeout(WAIT_SECONDS):
            ran = await run_next_job(app.state.sessions, mailer, now)

    attempts = {job.payload["booking_id"]: job.attempts for job in await stored_jobs()}
    assert held is not None
    assert held.payload["booking_id"] == booking_ids[3]
    assert ran
    assert attempts == {booking_ids[1]: 0, booking_ids[2]: 1, booking_ids[3]: 0}


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


async def test_the_worker_runs_jobs_in_sessions_that_postgres_ends_if_they_are_left_idle(
    app: FastAPI,
    worker_settings: WorkerSettings,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a job whose worker's machine vanished without closing its connection is freed."""
    limits: list[int] = []

    async def record_the_limit(session: AsyncSession, *_: object) -> None:
        limits.append(
            await session.scalar(
                text(
                    "SELECT setting::int FROM pg_settings"
                    " WHERE name = 'idle_in_transaction_session_timeout'"
                )
            )
        )

    monkeypatch.setattr(job_service, "_expire_hold", record_the_limit)
    await _queue_a_job_due_now(app)

    await _run_worker_until(worker_settings, lambda: _queue_is_empty(stored_jobs))

    assert limits == [int(worker.IDLE_IN_TRANSACTION_TIMEOUT.removesuffix("s")) * 1000]


async def test_the_worker_carries_on_after_an_error(
    app: FastAPI,
    worker_settings: WorkerSettings,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most likely the database was unreachable for a moment; the jobs waited in their table."""
    calls = 0

    async def fail_the_first_time(*args: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("The database is unreachable.")
        return await run_next_job(*args)

    async def do_nothing(*_: object) -> None:
        pass

    monkeypatch.setattr(worker, "run_next_job", fail_the_first_time)
    monkeypatch.setattr(job_service, "_expire_hold", do_nothing)
    await _queue_a_job_due_now(app)

    await _run_worker_until(worker_settings, lambda: _queue_is_empty(stored_jobs))

    assert calls >= 2


async def test_a_worker_told_to_stop_finishes_the_job_in_hand(
    app: FastAPI,
    worker_settings: WorkerSettings,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As `docker compose stop` asks it to: the job commits, and then the worker exits."""
    stopping = asyncio.Event()

    async def stop_halfway(*_: object) -> None:
        stopping.set()

    monkeypatch.setattr(job_service, "_expire_hold", stop_halfway)
    await _queue_a_job_due_now(app)

    async with asyncio.timeout(WAIT_SECONDS):
        await worker.run_worker(worker_settings, stopping)

    assert await stored_jobs() == []


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


async def _queue_a_job_due_now(app: FastAPI) -> None:
    """An expiry job for a booking that does not exist; the tests that run it replace its work."""
    async with app.state.sessions() as session, session.begin():
        payload = {"organisation_id": str(uuid4()), "booking_id": str(uuid4())}
        await jobs.enqueue(session, JobKind.EXPIRE_HOLD, payload, datetime.now(UTC))


async def _queue_is_empty(stored_jobs: StoredJobs) -> bool:
    return not await stored_jobs()


async def _count_refresh_tokens(app: FastAPI) -> int:
    async with app.state.sessions() as session:
        return await session.scalar(select(func.count()).select_from(RefreshTokenRecord)) or 0
