"""Fill an empty Holdfast database with a realistic volume of data for measuring queries.

The defaults build 200 organisations and about a million bookings: a year of history plus the 30
days ahead that the booking horizon allows. A few large tenants and many small ones make the data
as skewed as real use. Organisations, locations and resources are written through the
repositories, exactly as the API writes them. The high-volume tables are bulk loaded: users and
memberships in multi-row inserts, bookings and refresh tokens through COPY. Postgres checks every
constraint, the exclusion constraint included, and the booking rules hold because every period is
built with the domain's own code. Run it on an empty database migrated to the latest revision,
such as a fresh `make up`:

    uv run python scripts/seed.py [--organisations 200] [--history-days 365] [--database-url URL]
"""

import argparse
import asyncio
import itertools
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import perf_counter
from uuid import UUID, uuid7
from zoneinfo import ZoneInfo

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no type hints
from sqlalchemy import ForeignKeyConstraint, Table, UniqueConstraint, func, insert, select, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import AddConstraint, CreateIndex, DropConstraint, DropIndex

from holdfast.core.security import (
    ACCESS_TOKEN_LIFETIME,
    REFRESH_TOKEN_LIFETIME,
    hash_password,
    hash_refresh_token,
)
from holdfast.domain.bookings import BOOKING_HORIZON_DAYS, BookingStatus, price_for
from holdfast.domain.locations import Location, LocationDetails, OpeningHours, Weekday
from holdfast.domain.memberships import Role
from holdfast.domain.organisations import OrganisationDetails
from holdfast.domain.periods import GRID_MINUTES, Period
from holdfast.domain.resources import Resource, ResourceDetails, ResourceKind
from holdfast.repositories import locations, organisations, resources
from holdfast.repositories.records import Base, MembershipRecord, UserRecord

LOCAL_DATABASE_URL = "postgresql+asyncpg://holdfast:holdfast@127.0.0.1:54320/holdfast"
"""The Compose database, where compose.yaml publishes it on this machine."""
PASSWORD = "holdfast-seed-password"  # noqa: S105 - the documented password of every seeded user
RANDOM_SEED = 2026
"""Fixed, so that every run builds data of the same shape."""

REGIONS = (
    ("Europe/Amsterdam", "EUR"),
    ("Europe/London", "GBP"),
    ("America/New_York", "USD"),
    ("Asia/Colombo", "LKR"),
    ("Asia/Tokyo", "JPY"),
    ("Australia/Sydney", "AUD"),
)
WORKING_DAYS = [day for day in Weekday if day not in (Weekday.SATURDAY, Weekday.SUNDAY)]
OFFICE_HOURS: dict[Weekday, OpeningHours] = {
    day: OpeningHours(time(8), time(18)) for day in WORKING_DAYS
}
EXTENDED_HOURS: dict[Weekday, OpeningHours] = {
    day: OpeningHours(time(7), time(21)) for day in WORKING_DAYS
} | {Weekday.SATURDAY: OpeningHours(time(9), time(17))}

SLOT = timedelta(minutes=GRID_MINUTES)
MEETING_SLOTS = (2, 2, 4, 4, 4, 6, 8, 12)
"""Meeting lengths in grid slots: 30 minutes to 3 hours, most often an hour."""
MEETING_ROOM_SHARE = 0.3
CANCELLED_SHARE = 0.08
MEAN_LEAD_HOURS = 72
"""How long before its start a booking is typically made."""
SHARED_MEMBER_SHARE = 0.05
"""How often a new member is someone who already belongs to another organisation."""
BOOKER_SKEW = 0.7
"""Zipf exponent for who books: a few members book far more often than the rest."""
ACTIVE_USER_SHARE = 0.3
SIGN_INS_PER_ACTIVE_USER = 5
MEAN_REFRESHES = 20
LOGOUT_SHARE = 0.25

BOOKING_COLUMNS = (
    "organisation_id",
    "resource_id",
    "user_id",
    "period",
    "status",
    "price",
    "created_at",
)
REFRESH_TOKEN_COLUMNS = (
    "user_id",
    "family_id",
    "token_hash",
    "expires_at",
    "used_at",
    "revoked_at",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class Tier:
    """The size of an organisation and how busy its resources are."""

    locations: int
    resources_per_location: int
    members: int
    busyness: float
    """For a desk, the chance that it is booked on an open day; for a meeting room, the share of
    its opening hours that is booked."""


LARGE = Tier(locations=5, resources_per_location=40, members=1500, busyness=0.45)
MEDIUM = Tier(locations=3, resources_per_location=15, members=200, busyness=0.35)
SMALL = Tier(locations=1, resources_per_location=6, members=25, busyness=0.23)


@dataclass(frozen=True, slots=True)
class Bookable:
    """A resource, with what it takes to generate its bookings."""

    resource: Resource
    busyness: float
    bookers: Sequence[UUID]
    cumulative_weights: Sequence[float]


@dataclass(frozen=True, slots=True)
class Site:
    """A location and its resources, which all share its opening hours."""

    location: Location
    local_today: date
    bookables: list[Bookable]


class NotEmptyError(Exception):
    """The database already holds data, which the seed must never mix with."""


async def seed(database_url: str, organisation_count: int, history_days: int) -> dict[str, int]:
    """Fill the empty database at `database_url` and return the number of rows in each table."""
    rng = random.Random(RANDOM_SEED)
    now = datetime.now(UTC)
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            if await session.scalar(select(UserRecord.id).limit(1)) is not None:
                raise NotEmptyError(
                    "The database already holds data. Seed an empty one, for example after "
                    "`docker compose down --volumes` and `make up`."
                )
            user_ids, sites = await _add_organisations(session, rng, organisation_count, now)
            tables = Base.metadata.tables
            bookings = _bookings(rng, sites, history_days, now)
            await _bulk_load(session, tables["bookings"], BOOKING_COLUMNS, bookings)
            refresh_tokens = _refresh_tokens(rng, user_ids, now)
            await _bulk_load(
                session, tables["refresh_tokens"], REFRESH_TOKEN_COLUMNS, refresh_tokens
            )
        async with engine.connect() as connection:
            # Set the visibility map and gather statistics now, as autovacuum would in time.
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text("VACUUM ANALYZE"))
            return {
                table.name: (
                    await autocommit.execute(select(func.count()).select_from(table))
                ).scalar_one()
                for table in Base.metadata.sorted_tables
            }
    finally:
        await engine.dispose()


async def _bulk_load(
    session: AsyncSession, table: Table, columns: Sequence[str], rows: Iterable[tuple[object, ...]]
) -> None:
    """COPY rows into a table as Postgres's guide to populating a database advises: drop its
    foreign keys, unique and exclusion constraints and indexes first, and add them back after.
    Each index is then built in one pass and each foreign key checked by one query, instead of
    both happening row by row. Adding them back still checks every row, and if anything fails,
    the transaction rolls back the schema changes along with the data.
    """
    deferred = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint | UniqueConstraint | ExcludeConstraint)
    ]
    for constraint in deferred:
        await session.execute(DropConstraint(constraint))
    for index in table.indexes:
        await session.execute(DropIndex(index))
    # COPY belongs to the asyncpg connection underneath the session, inside its transaction.
    raw_connection = await (await session.connection()).get_raw_connection()
    driver = raw_connection.driver_connection
    if driver is None:
        raise RuntimeError("The session has no open database connection.")
    await driver.copy_records_to_table(table.name, records=rows, columns=columns)
    for index in table.indexes:
        await session.execute(CreateIndex(index))
    for constraint in deferred:
        await session.execute(AddConstraint(constraint))


async def _add_organisations(
    session: AsyncSession, rng: random.Random, count: int, now: datetime
) -> tuple[list[UUID], list[Site]]:
    """Add the organisations with their locations, resources and members. Return every user's
    id, and the sites whose bookings are still to be generated."""
    user_ids: list[UUID] = []
    memberships: list[dict[str, object]] = []
    sites: list[Site] = []
    for index in range(1, count + 1):
        tier = _tier(index)
        timezone, currency = REGIONS[index % len(REGIONS)]
        details = OrganisationDetails(name=f"Organisation {index:03}", slug=f"org-{index:03}")
        organisation = await organisations.add(session, details, currency)
        members = _pick_members(rng, tier, user_ids)
        memberships += _memberships(organisation.id, tier, members)
        bookers = rng.sample(members, len(members))
        weights = list(
            itertools.accumulate(1 / rank**BOOKER_SKEW for rank in range(1, len(bookers) + 1))
        )
        for location_number in range(1, tier.locations + 1):
            location = await locations.add(
                session,
                organisation.id,
                LocationDetails(
                    name=f"Location {location_number}",
                    address=f"{location_number} Example Street",
                    timezone=ZoneInfo(timezone),
                    opening_hours=EXTENDED_HOURS if rng.random() < 0.25 else OFFICE_HOURS,
                ),
            )
            site = Site(location, now.astimezone(location.timezone).date(), bookables=[])
            for resource_number in range(1, tier.resources_per_location + 1):
                resource = await resources.add(
                    session, organisation.id, location.id, _resource_details(rng, resource_number)
                )
                busyness = min(0.9, tier.busyness * rng.uniform(0.5, 1.5))
                site.bookables.append(Bookable(resource, busyness, bookers, weights))
            sites.append(site)
    password_hash = await hash_password(PASSWORD)
    users = [
        {"id": user_id, "email": f"user-{number:05}@example.com", "password_hash": password_hash}
        for number, user_id in enumerate(user_ids, start=1)
    ]
    await session.execute(insert(UserRecord), users)
    await session.execute(insert(MembershipRecord), memberships)
    return user_ids, sites


def _tier(index: int) -> Tier:
    """Every 50th organisation is large and every 5th medium: a few big tenants, many small."""
    if index % 50 == 0:
        return LARGE
    if index % 5 == 0:
        return MEDIUM
    return SMALL


def _pick_members(rng: random.Random, tier: Tier, user_ids: list[UUID]) -> list[UUID]:
    """The tier's number of members: mostly new users, appended to `user_ids`, and some who
    belong to another organisation too."""
    members: dict[UUID, None] = {}
    while len(members) < tier.members:
        if user_ids and rng.random() < SHARED_MEMBER_SHARE:
            user_id = rng.choice(user_ids)
        else:
            user_id = uuid7()
            user_ids.append(user_id)
        members[user_id] = None
    return list(members)


def _memberships(
    organisation_id: UUID, tier: Tier, members: Sequence[UUID]
) -> list[dict[str, object]]:
    """The first members own the organisation, the next few administer it."""
    owners = 1 if tier is SMALL else 2
    admins = max(1, tier.members // 25)
    return [
        {
            "organisation_id": organisation_id,
            "user_id": user_id,
            "role": Role.OWNER
            if rank < owners
            else Role.ADMIN
            if rank < owners + admins
            else Role.MEMBER,
        }
        for rank, user_id in enumerate(members)
    ]


def _resource_details(rng: random.Random, number: int) -> ResourceDetails:
    if rng.random() < MEETING_ROOM_SHARE:
        return ResourceDetails(
            name=f"Room {number}",
            kind=ResourceKind.MEETING_ROOM,
            capacity=rng.choice((4, 6, 8, 12)),
            hourly_price=rng.choice((2000, 3500, 5000, 8000)),
        )
    return ResourceDetails(
        name=f"Desk {number}",
        kind=ResourceKind.DESK,
        capacity=1,
        hourly_price=rng.choice((0, 500, 1000)),
    )


def _bookings(
    rng: random.Random, sites: Sequence[Site], history_days: int, now: datetime
) -> Iterator[tuple[object, ...]]:
    """Booking rows a day at a time, each day's in random order, so that every resource's
    bookings end up spread through the table as they would after months of real traffic."""
    for offset in range(-history_days, BOOKING_HORIZON_DAYS + 1):
        # The further ahead a day is, the fewer of its bookings have been made yet.
        made_yet = 1 - max(offset, 0) / (BOOKING_HORIZON_DAYS + 1)
        rows = []
        for site in sites:
            opening = site.location.opening_period(site.local_today + timedelta(days=offset))
            if opening is None:
                continue
            for bookable in site.bookables:
                for period in _day_of_bookings(rng, bookable, opening):
                    if rng.random() < made_yet:
                        rows.append(_booking_row(rng, bookable, period, now))
        rng.shuffle(rows)
        yield from rows


def _day_of_bookings(rng: random.Random, bookable: Bookable, opening: Period) -> Iterable[Period]:
    if bookable.resource.kind is ResourceKind.DESK:
        return _desk_day(rng, opening, bookable.busyness)
    return _meetings(rng, opening, bookable.busyness)


def _desk_day(rng: random.Random, opening: Period, busyness: float) -> list[Period]:
    """A desk is booked for the whole day, for one or both halves of it, or not at all."""
    if rng.random() >= busyness:
        return []
    if rng.random() < 0.5:
        return [opening]
    middle = opening.start + SLOT * (opening.duration // SLOT // 2)
    halves = (Period(opening.start, middle), Period(middle, opening.end))
    return [half for half in halves if rng.random() < 0.7]


def _meetings(rng: random.Random, opening: Period, busyness: float) -> Iterator[Period]:
    """Back-to-back meetings separated by random gaps, filling about `busyness` of the day."""
    mean_gap = sum(MEETING_SLOTS) / len(MEETING_SLOTS) * (1 - busyness) / busyness
    start = opening.start
    while True:
        start += SLOT * round(rng.expovariate(1 / mean_gap))
        end = start + SLOT * rng.choice(MEETING_SLOTS)
        if end > opening.end:
            return
        yield Period(start, end)
        start = end


def _booking_row(
    rng: random.Random, bookable: Bookable, period: Period, now: datetime
) -> tuple[object, ...]:
    booked_by = rng.choices(bookable.bookers, cum_weights=bookable.cumulative_weights)[0]
    status = BookingStatus.CANCELLED if rng.random() < CANCELLED_SHARE else BookingStatus.CONFIRMED
    lead = timedelta(hours=rng.expovariate(1 / MEAN_LEAD_HOURS))
    return (
        bookable.resource.organisation_id,
        bookable.resource.id,
        booked_by,
        asyncpg.Range(period.start, period.end),
        status,
        price_for(period, bookable.resource.hourly_price),
        min(period.start - lead, now),
    )


def _refresh_tokens(
    rng: random.Random, user_ids: Sequence[UUID], now: datetime
) -> Iterator[tuple[object, ...]]:
    """Token rows for sign-ins over the last 30 days. Each sign-in is a family with one token per
    refresh, all used except the newest; a logout revokes the whole family."""
    for user_id in user_ids:
        if rng.random() >= ACTIVE_USER_SHARE:
            continue
        for _ in range(rng.randint(1, 2 * SIGN_INS_PER_ACTIVE_USER - 1)):
            family_id = uuid7()
            signed_in_at = now - REFRESH_TOKEN_LIFETIME * rng.random()
            refreshes = round(rng.expovariate(1 / MEAN_REFRESHES))
            issued = [signed_in_at + ACCESS_TOKEN_LIFETIME * n for n in range(refreshes + 1)]
            issued = [moment for moment in issued if moment <= now]
            logged_out = rng.random() < LOGOUT_SHARE
            revoked_at = min(issued[-1] + ACCESS_TOKEN_LIFETIME, now) if logged_out else None
            for issued_at, next_issued_at in itertools.zip_longest(issued, issued[1:]):
                yield (
                    user_id,
                    family_id,
                    hash_refresh_token(rng.randbytes(32).hex()),
                    issued_at + REFRESH_TOKEN_LIFETIME,
                    next_issued_at,
                    revoked_at,
                    issued_at,
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill an empty Holdfast database with volume.")
    parser.add_argument("--database-url", default=LOCAL_DATABASE_URL)
    parser.add_argument("--organisations", type=int, default=200)
    parser.add_argument("--history-days", type=int, default=365)
    arguments = parser.parse_args()
    started = perf_counter()
    try:
        counts = asyncio.run(
            seed(arguments.database_url, arguments.organisations, arguments.history_days)
        )
    except NotEmptyError as error:
        raise SystemExit(error) from None
    for table, count in counts.items():
        print(f"{table:<15} {count:>10,}")
    print(f"Seeded in {perf_counter() - started:.0f} seconds.")


if __name__ == "__main__":
    main()
