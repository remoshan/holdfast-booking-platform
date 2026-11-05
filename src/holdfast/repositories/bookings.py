"""Persistence for bookings. Every query is scoped to one organisation or one resource."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Range, insert
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import INACTIVE_STATUSES, Booking, BookingStatus
from holdfast.domain.errors import BookingConflictError, NotFoundError
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import NO_OVERLAP_CONSTRAINT, BookingRecord


async def add(
    session: AsyncSession,
    organisation_id: UUID,
    resource_id: UUID,
    user_id: UUID,
    period: Period,
    price: int,
) -> Booking:
    """Insert a confirmed booking unless it overlaps an active booking of the same resource.

    The exclusion constraint decides, so concurrent requests can never both win. Naming it in
    ON CONFLICT DO NOTHING turns a lost race into "no row inserted". A plain INSERT would make
    colliding transactions wait for each other, and Postgres would abort some as deadlocks.
    """
    record = await session.scalar(
        insert(BookingRecord)
        .values(
            organisation_id=organisation_id,
            resource_id=resource_id,
            user_id=user_id,
            period=_to_range(period),
            status=BookingStatus.CONFIRMED,
            price=price,
        )
        .on_conflict_do_nothing(constraint=NO_OVERLAP_CONSTRAINT)
        .returning(BookingRecord)
    )
    if record is None:
        raise BookingConflictError("The resource is already booked for part of this period.")
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
            BookingRecord.status.not_in(INACTIVE_STATUSES),
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
