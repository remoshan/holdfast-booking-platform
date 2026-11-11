"""Persistence for bookings. Every query is scoped to one organisation, one resource or the
bookings of one user."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Function, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import Range, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from holdfast.domain.bookings import INACTIVE_STATUSES, Booking, BookingStatus
from holdfast.domain.errors import BookingConflictError, NotFoundError
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories.pagination import fetch_page, fetch_sorted_page
from holdfast.repositories.records import BookingRecord, MembershipRecord


async def add(
    session: AsyncSession,
    organisation_id: UUID,
    resource_id: UUID,
    user_id: UUID,
    period: Period,
    price: int,
    hold_until: datetime | None = None,
    idempotency_key: str | None = None,
) -> Booking:
    """Insert a booking unless it overlaps an active booking of the same resource: a hold that
    lapses at `hold_until`, or a confirmed booking if that is None.

    The exclusion constraint decides, so concurrent requests can never both win. ON CONFLICT DO
    NOTHING turns a lost race into "no row inserted". A plain INSERT would make colliding
    transactions wait for each other, and Postgres would abort some as deadlocks.

    Nothing is inserted either when the user's idempotency key already has its booking, which
    is how a request that arrives twice at the same moment ends. The caller tells the two
    cases apart by looking that booking up.
    """
    record = await session.scalar(
        insert(BookingRecord)
        .values(
            organisation_id=organisation_id,
            resource_id=resource_id,
            user_id=user_id,
            period=_to_range(period),
            status=BookingStatus.CONFIRMED if hold_until is None else BookingStatus.HELD,
            price=price,
            expires_at=hold_until,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing()
        .returning(BookingRecord)
    )
    if record is None:
        raise BookingConflictError("The resource is already booked for part of this period.")
    return _to_booking(record)


async def get(session: AsyncSession, organisation_id: UUID, booking_id: UUID) -> Booking:
    return await _get(session, organisation_id, booking_id, lock=False)


async def lock(session: AsyncSession, organisation_id: UUID, booking_id: UUID) -> Booking:
    """Return the booking with its row locked until the transaction ends, so that confirming,
    cancelling and expiring it happen one at a time."""
    return await _get(session, organisation_id, booking_id, lock=True)


async def find_by_idempotency_key(
    session: AsyncSession, user_id: UUID, idempotency_key: str
) -> Booking | None:
    """The booking that the user's request with this idempotency key made, if it made one."""
    record = await session.scalar(
        select(BookingRecord).where(
            BookingRecord.user_id == user_id, BookingRecord.idempotency_key == idempotency_key
        )
    )
    return None if record is None else _to_booking(record)


async def list_page(
    session: AsyncSession,
    organisation_id: UUID,
    resource_id: UUID,
    booked_by: UUID | None,
    after: UUID | None,
    limit: int,
) -> Page[Booking]:
    """A page of the resource's bookings, only those of `booked_by` unless it is None."""
    query = select(BookingRecord).where(
        BookingRecord.organisation_id == organisation_id,
        BookingRecord.resource_id == resource_id,
    )
    if booked_by is not None:
        query = query.where(BookingRecord.user_id == booked_by)
    return await fetch_page(session, query, BookingRecord.id, after, limit, _to_booking)


async def list_upcoming(
    session: AsyncSession, user_id: UUID, now: datetime, after: UUID | None, limit: int
) -> Page[Booking]:
    """A page of the user's active bookings that have not ended by `now`, soonest first, in the
    organisations they still belong to.

    The list is sorted by start, then id. Its cursor is the id of the previous page's last
    booking, like every other list's, and that booking's start is looked up to continue after it.
    """
    start = _start_of(BookingRecord.period)
    query = select(BookingRecord).where(
        BookingRecord.user_id == user_id,
        BookingRecord.status.not_in(INACTIVE_STATUSES),
        _end_of(BookingRecord.period) > now,
        exists().where(
            MembershipRecord.organisation_id == BookingRecord.organisation_id,
            MembershipRecord.user_id == user_id,
        ),
    )
    if after is not None:
        previous = aliased(BookingRecord)
        previous_start = (
            select(_start_of(previous.period))
            .where(previous.id == after, previous.user_id == user_id)
            .scalar_subquery()
        )
        query = query.where(tuple_(start, BookingRecord.id) > tuple_(previous_start, after))
    sorted_query = query.order_by(start, BookingRecord.id)
    return await fetch_sorted_page(session, sorted_query, BookingRecord.id, limit, _to_booking)


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


async def _get(
    session: AsyncSession, organisation_id: UUID, booking_id: UUID, *, lock: bool
) -> Booking:
    query = select(BookingRecord).where(
        BookingRecord.organisation_id == organisation_id, BookingRecord.id == booking_id
    )
    record = await session.scalar(query.with_for_update() if lock else query)
    if record is None:
        raise NotFoundError("Booking", booking_id)
    return _to_booking(record)


def _start_of(period: InstrumentedAttribute[Range[datetime]]) -> Function[datetime]:
    return func.lower(period, type_=DateTime(timezone=True))


def _end_of(period: InstrumentedAttribute[Range[datetime]]) -> Function[datetime]:
    return func.upper(period, type_=DateTime(timezone=True))


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
        expires_at=record.expires_at,
        created_at=record.created_at,
    )
