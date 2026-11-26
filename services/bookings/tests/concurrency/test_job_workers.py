"""Workers racing for jobs. A worker killed in the middle of one is tested with the notifications
service, whose jobs send email."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import Response

from holdfast.bookings.domain.reminders import REMINDER_LEAD
from holdfast.bookings.services import jobs
from holdfast.messaging.jobs import run_next_job

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Published = Callable[[], Awaitable[list[tuple[str, Json]]]]

RACING_WORKERS = 4


def _clock(minutes_after_opening: int) -> str:
    hours, minutes = divmod(8 * 60 + minutes_after_opening, 60)
    return f"{hours:02}:{minutes:02}"


async def test_racing_workers_run_each_job_exactly_once(
    app: FastAPI, book: Book, free_resource: Json, published: Published
) -> None:
    # Forty back-to-back 15-minute slots fill the opening hours: forty reminders to send.
    slots = [(_clock(offset), _clock(offset + 15)) for offset in range(0, 10 * 60, 15)]
    booked = [
        (await book(start, end, resource_id=free_resource["id"])).json() for start, end in slots
    ]
    # By then every reminder is due, and no booking has started.
    now = datetime.fromisoformat(booked[-1]["start"]) - REMINDER_LEAD

    async def work() -> int:
        jobs_run = 0
        while await run_next_job(app.state.sessions, jobs.HANDLERS, now):
            jobs_run += 1
        return jobs_run

    jobs_run = await asyncio.gather(*(work() for _ in range(RACING_WORKERS)))

    reminded = [
        data["booking_id"] for event, data in await published() if event == "bookings.reminder_due"
    ]
    assert sum(jobs_run) == len(booked)
    assert Counter(reminded) == Counter(booking["id"] for booking in booked)
