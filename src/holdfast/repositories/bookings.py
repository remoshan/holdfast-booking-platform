"""Persistence for bookings. Every query is scoped to one organisation or one resource."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import ACTIVE_STATUSES, Booking, BookingStatus
from holdfast.domain.errors import NotFoundError
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import BookingRecord


async def add(
    session: AsyncSession,
    organisation_id: UUID,
    resource_id: UUID,
    user_id: UUID,
    period: Period,
    price: int,
) -> Booking:
    record = BookingRecord(
        organisation_id=organisation_id,
        resource_id=resource_id,
        user_id=user_id,
        period=_to_range(period),
        status=BookingStatus.CONFIRMED,
        price=price,
    )
    session.add(record)
    await session.flush()
    return _to_booking(record)


async def get(session: AsyncSession, organisation_id: UUID, booking_id: UUID) -> Booking:
    record = await session.scalar(
        select(BookingRecord).where(
            BookingRecord.organisation_id == organisation_id, BookingRecord.id == booking_id
        )
    )
    if record is None:
        raise NotFoundError("Booking", booking_id)
    return _to_booking(record)


async def list_page(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, after: UUID | None, limit: int
) -> Page[Booking]:
    query = select(BookingRecord).where(
        BookingRecord.organisation_id == organisation_id,
        BookingRecord.resource_id == resource_id,
    )
    return await fetch_page(session, query, BookingRecord, after, limit, _to_booking)


async def active_periods(session: AsyncSession, resource_id: UUID, window: Period) -> list[Period]:
    """The periods of the resource's active bookings that overlap the window."""
    ranges = await session.scalars(
        select(BookingRecord.period).where(
            BookingRecord.resource_id == resource_id,
            BookingRecord.status.in_(ACTIVE_STATUSES),
            BookingRecord.period.overlaps(_to_range(window)),
        )
    )
    return [_to_period(booked) for booked in ranges]


async def save_status(session: AsyncSession, booking: Booking) -> None:
    await session.execute(
        update(BookingRecord).where(BookingRecord.id == booking.id).values(status=booking.status)
    )


def _to_range(period: Period) -> Range[datetime]:
    return Range(period.start, period.end, bounds="[)")


def _to_period(booked: Range[datetime]) -> Period:
    if booked.lower is None or booked.upper is None:
        raise ValueError("Booking periods are always bounded.")
    return Period(booked.lower, booked.upper)


def _to_booking(record: BookingRecord) -> Booking:
    return Booking(
        id=record.id,
        organisation_id=record.organisation_id,
        resource_id=record.resource_id,
        user_id=record.user_id,
        period=_to_period(record.period),
        status=record.status,
        price=record.price,
        created_at=record.created_at,
    )
