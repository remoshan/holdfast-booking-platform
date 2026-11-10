# Holdfast

[![CI](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A multi-tenant booking platform for shared spaces. Organisations list desks and meeting rooms, members reserve time slots, and the system guarantees no double-booking, even when fifty people click "Book" on the same room at the same moment.

The name is the promise: a reservation hold that holds fast.

## Guarantees

1. **No double-booking.** No two active bookings overlap on the same resource. A Postgres exclusion constraint enforces this, so it holds however many requests race for the same slot.
2. **Tenant isolation.** A user can never read or change another organisation's data. Every request carries a signed access token, every query on tenant data is scoped to an organisation the caller belongs to, and requests from outside it return `404`, so they leak nothing.
3. **Holds expire.** From Phase 5, a hold that is not paid for in time expires and frees the slot.

## Roadmap

Each phase ends with a deployable system, a merged pull request and its architecture decisions recorded below.

| Phase | Focus | Status |
| --- | --- | --- |
| 0 | Foundations: Compose, CI, migrations, health check | Done |
| 1 | Core API: CRUD, availability search, problem+json errors | Done |
| 2 | No double-booking: exclusion constraint, concurrency tests | Done |
| 3 | Authentication and per-organisation roles | Done |
| 4 | Data under pressure: indexes, query plans, pagination at depth | Done |
| 5 | Background work: expiring holds, job queue, idempotency | Planned |
| 6 | Payment saga, outbox, two extracted services | Planned |
| 7 | Hardening: OWASP review, rate limits, tracing | Planned |

## Tech stack

| Concern | Choice |
| --- | --- |
| Language and framework | Python 3.14, FastAPI |
| Database | PostgreSQL 18 |
| Data access and migrations | SQLAlchemy 2.1 (async, asyncpg), Alembic |
| Settings and validation | Pydantic v2, pydantic-settings |
| Authentication | JWT access tokens (PyJWT), Argon2id password hashing (argon2-cffi) |
| Testing | pytest, HTTPX, Testcontainers (real Postgres, never SQLite) |
| Quality gates | Ruff, mypy (strict), import-linter, pre-commit |
| Packaging | uv |
| Local environment | Docker Compose, Make |
| CI | GitHub Actions |

## Getting started

Prerequisites: [Docker](https://docs.docker.com/get-docker/), [uv](https://docs.astral.sh/uv/) and GNU Make.

```bash
git clone https://github.com/remoshan/holdfast-booking-platform.git
cd holdfast-booking-platform
make install   # create the virtualenv and install the pre-commit hooks
make up        # build and start Postgres, run migrations, start the API
```

The API is then available at <http://localhost:8000>, with interactive OpenAPI docs at <http://localhost:8000/docs>. To try it there, register with `POST /auth/register`, log in with `POST /auth/login`, then choose **Authorize** and paste the `access_token`. Stop everything with `make down`.

Postgres listens on `127.0.0.1:54320` for local tools, with `holdfast` as user, password and database. The uncommon port avoids any other Postgres on your machine, and the server is not reachable from the network.

### Commands

| Command | What it does |
| --- | --- |
| `make install` | Install dependencies and the pre-commit hooks |
| `make up` / `make down` | Start or stop the full stack in Docker |
| `make migrate` | Apply new migrations to the running database |
| `make seed` | Fill the empty local database with a million bookings, in about three minutes |
| `make explain` | Measure the hot queries with `EXPLAIN ANALYZE`, before and after the Phase 4 indexes |
| `make format` | Format code and apply safe lint fixes |
| `make lint` | Ruff lint and format check, plus the layer import rules |
| `make typecheck` | mypy in strict mode |
| `make test` | Run the test suite (needs Docker for Testcontainers) |
| `make check` | Lint, type-check and test: exactly what CI runs |

### Configuration

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy URL with the asyncpg driver, e.g. `postgresql+asyncpg://user:password@host:5432/holdfast` |
| `JWT_SECRET` | Key that signs access tokens: at least 32 random characters, different in every environment. Compose sets a value for local development only. |

## API

Every path that belongs to a tenant starts with `/organisations/{organisation_id}`, shortened to `…` below. Every endpoint except `/health` and `/auth/*` needs an access token. The running API documents every request and response at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Report whether the API can reach its database |
| `POST` | `/auth/register` | Create an account |
| `POST` | `/auth/login` | Exchange an email and password for tokens |
| `POST` | `/auth/refresh` | Exchange a refresh token for new tokens |
| `POST` | `/auth/logout` | End the sign-in a refresh token belongs to |
| `GET` | `/users/me` | Get the signed-in user |
| `GET` | `/users/me/bookings` | List your bookings that have not ended, soonest first, in every organisation you belong to |
| `POST`, `GET` | `/organisations` | Create an organisation, or list your own |
| `GET`, `PUT`, `DELETE` | `/organisations/{organisation_id}` | Get, replace or delete an organisation |
| `POST`, `GET` | `…/members` | Add a member or list members |
| `GET`, `PUT`, `DELETE` | `…/members/{user_id}` | Get a member, change their role or remove them |
| `POST`, `GET` | `…/locations` | Create or list locations |
| `GET`, `PUT`, `DELETE` | `…/locations/{location_id}` | Get, replace or delete a location |
| `POST`, `GET` | `…/locations/{location_id}/resources` | Create or list a location's resources |
| `GET`, `PUT`, `DELETE` | `…/resources/{resource_id}` | Get, replace or delete a resource |
| `GET` | `…/resources/{resource_id}/availability?date=YYYY-MM-DD` | Free periods on a day in the location's time zone |
| `POST`, `GET` | `…/resources/{resource_id}/bookings` | Book a resource or list its bookings |
| `GET` | `…/bookings/{booking_id}` | Get a booking |
| `POST` | `…/bookings/{booking_id}/cancel` | Cancel a booking |

### Conventions

- **Authentication.** Logging in returns an access token that lasts 15 minutes and a refresh token. Send the access token as `Authorization: Bearer <token>`. A refresh token works once: `POST /auth/refresh` returns a new pair, and sending a used refresh token again ends that whole sign-in.
- **Updates.** `PUT` replaces every editable field. Deleting an organisation, location or resource that still has locations, resources or bookings is refused with `409`.
- **Time.** Request timestamps must include a UTC offset, and responses are in UTC. Opening hours and booking rules use the location's time zone.
- **Money.** Prices are integers in minor units, such as cents, of the organisation's currency. The currency is set when the organisation is created and never changes.
- **Pagination.** Lists return `{"items": [...], "next_cursor": "..."}`. Pass `next_cursor` back as `cursor` for the next page; it is `null` on the last page. `limit` is 1 to 100 and defaults to 20. Items come in the order they were created, except your bookings, which come soonest first.

### Roles

Each member of an organisation has one role, and each role may do everything the roles to its left may.

| | Member | Admin | Owner |
| --- | :-: | :-: | :-: |
| See the organisation, its locations, resources and availability | ✓ | ✓ | ✓ |
| Book, and see or cancel their own bookings | ✓ | ✓ | ✓ |
| See and cancel everyone's bookings | | ✓ | ✓ |
| Manage locations and resources | | ✓ | ✓ |
| See the member list, and add or remove plain members | | ✓ | ✓ |
| Change roles, and add or remove admins and owners | | | ✓ |
| Rename or delete the organisation | | | ✓ |

Whoever creates an organisation becomes its owner, and an organisation always keeps at least one owner. Anyone outside the organisation gets `404` for everything in it. A member whose role is too low gets `403`, except that other people's bookings simply look missing (`404`) to a plain member.

### Booking rules

A booking must:

1. start and end on a 15-minute boundary in the location's local time;
2. fall within the location's opening hours on a single day, so there are no overnight bookings;
3. start no earlier than now and at most 30 days from today;
4. not overlap an active booking of the same resource. Postgres enforces this rule itself, so it holds even when many requests arrive at once.

A booking costs the resource's hourly price for the time that actually elapses, rounded half up to a whole minor unit. It can be cancelled until it starts, and cancelling it again changes nothing.

### Errors

Every error is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details document, sent as `application/problem+json`:

```json
{
  "type": "https://github.com/remoshan/holdfast-booking-platform#invalid-booking",
  "title": "Booking rule broken",
  "status": 422,
  "detail": "Bookings must start and end on a 15-minute boundary.",
  "instance": "/organisations/0199.../resources/0199.../bookings"
}
```

Errors that the HTTP status explains fully, such as `401 Unauthorized`, `403 Forbidden`, `404 Not Found` or a `409 Conflict` for a taken slug, use the type `about:blank`; every `401` also carries `WWW-Authenticate: Bearer`. These problem types carry more meaning:

#### validation-error

`422`: the request is malformed. `errors` lists every invalid input, with a JSON `pointer` into the body or the name of the `parameter` or `header`.

#### invalid-booking

`422`: the booking breaks one of the booking rules; `detail` says which one.

#### booking-conflict

`409`: the resource is already booked for part of the requested period.

## Testing

`make test` runs three suites; `uv run pytest tests/<suite>` runs one of them.

| Suite | What it proves |
| --- | --- |
| `tests/unit` | Booking rules, availability, pricing, roles and tokens, including daylight-saving changes. Runs in milliseconds without a database. |
| `tests/integration` | Every endpoint through HTTP against a real Postgres, both when the request succeeds and when it fails. A permission matrix calls every organisation endpoint as an anonymous caller, an outsider, a member, an admin and an owner, and fails if a new endpoint is missing from it. The seed script runs here too, at a small scale. |
| `tests/concurrency` | That exactly one of 50 simultaneous requests for a slot wins, and that 20 inserts released at the same instant end in one booking and clean conflicts, never deadlocks. |

## Performance

Phase 4 measured every hot query against realistic volume and added only the indexes that the numbers justified (ADR-0011). To reproduce the measurements:

```bash
make up        # a fresh stack, migrated to the latest revision
make seed      # about three minutes
make explain   # every query below, before and after the Phase 4 indexes
```

`make seed` builds 200 organisations from a fixed random seed, so every run has the same shape: a few large tenants and many small ones, a year of history and the 30 days ahead that the booking horizon allows. It refuses a database that already holds data. The seeded users are `user-00001@example.com` onwards, all with the password `holdfast-seed-password`.

| Table | Rows |
| --- | ---: |
| Bookings, 8% of them cancelled | 1,029,604 |
| Refresh tokens | 503,314 |
| Memberships | 17,200 |
| Users | 16,313 |
| Resources | 3,380 |
| Locations | 288 |
| Organisations | 200 |

`make explain` runs the exact SQL the application sends, captured from SQLAlchemy, with the heaviest parameters the data holds, such as the busiest resource and the user with the most bookings. "Before" drops the Phase 4 indexes inside a transaction that is rolled back, so both sides run on the same data. Each time is the median of seven warm runs on a development machine, in Docker Desktop on Windows. Times vary from run to run, the slow ones by as much as double, but the plans stay the same.

| Query | Before | After | Served by |
| --- | ---: | ---: | --- |
| My upcoming bookings, for the user with 3,123 bookings | 117.6 ms | 1.10 ms | `ix_bookings_active_user_id_end` |
| A resource's bookings, first page | 21.5 ms | 0.17 ms | `ix_bookings_resource_id_id` |
| The same list for a plain member, who sees only their own | 85.3 ms | 5.02 ms | `ix_bookings_resource_id_id` |
| Delete a resource that has no bookings | 154.4 ms | 0.42 ms | `ix_bookings_resource_id_id` |
| My organisations | 2.18 ms | 0.32 ms | `ix_memberships_user_id` |
| Log out of a sign-in with 202 refresh tokens | 112.3 ms | 9.04 ms | `ix_refresh_tokens_family_id` |
| A resource's availability on its busiest day | 0.21 ms | 0.20 ms | The exclusion constraint's index |
| Book a free slot | 0.85 ms | 0.70 ms | Writes two more index entries |

### What each index is for

- **`ix_bookings_active_user_id_end`** on `(user_id, upper(period)) WHERE status <> 'cancelled'`, 36 MB. "My upcoming bookings" wants a user's active bookings that have not ended. Without it, Postgres scanned all million bookings in parallel. Keyed by end time, the unfinished bookings form one short range at the end of each user's entries, so Postgres reads only those: 118 of the busiest user's 3,123, never more than the 30-day horizon allows, then sorts them by start. Keyed by start time, it could not tell where the unfinished ones begin and would read the user's whole history. Cancelled bookings never appear in the list, so the index leaves them out; its condition and the exclusion constraint's come from one constant in `records.py`.
- **`ix_bookings_resource_id_id`** on `(resource_id, id)`, 49 MB. A page of a resource's bookings walks this index from the cursor and stops after 21 entries, reading 24 buffers. Without it, Postgres walked the primary key and discarded 21,000 bookings of other resources. It also serves the foreign key from bookings to resources: Postgres does not index foreign keys by itself, so deleting an unused resource scanned every booking to prove that none referred to it. A plain member's view still reads all of the resource's bookings to find their own. That is 5 ms on the busiest resource, and members have `/users/me/bookings` for their own list, so it gets no index of its own.
- **`ix_memberships_user_id`** on `(user_id)`, 528 kB. The primary key `(organisation_id, user_id)` cannot find one user's memberships, so listing your organisations scanned all 17,200 memberships, a cost that grows with every member of every organisation.
- **`ix_refresh_tokens_family_id`** on `(family_id)`, 4 MB, small because B-tree deduplication stores a repeated family id once, with a list of the rows that hold it. A logout or a replayed token revokes the whole family; without it, Postgres scanned all 503,314 tokens. Finding the 202 tokens now takes 0.6 ms. The rest is writing 202 new row versions, and a typical sign-in has only 15 tokens.

**Availability needs no new index.** The exclusion constraint keeps a GiST index on `(resource_id, period)` for active bookings, which is exactly what availability asks: an index-only scan that reads 5 buffers.

**What the indexes cost.** The two new booking indexes add 85 MB to a 139 MB table, and each booking writes two more index entries: three more buffers touched per insert, a difference lost in the noise of a single insert. Migration 0005 builds the indexes with `CREATE INDEX CONCURRENTLY`, so bookings keep flowing while it runs; on this data it took 18 seconds.

### Offset versus keyset pagination

The busiest resource has 2,013 bookings, so the last page of its list is about 100 pages deep.

| Depth | `OFFSET` | Keyset |
| ---: | ---: | ---: |
| 0 | 0.17 ms | 0.12 ms |
| 1,006 | 7.08 ms | 0.18 ms |
| 1,993 | 9.09 ms | 0.31 ms |

`OFFSET` has to produce every row before the page and throw it away. For a deep page Postgres stops following the index and instead fetches all of the resource's bookings, spread over about 1,900 table pages, to sort them, so the cost grows with the list. A keyset page asks for the rows after the last one seen, which the index finds directly, so every page reads about 25 buffers however deep it is. Every list in the API pages by keyset (ADR-0010).

## Architecture

Holdfast is a modular monolith with strict layers. Each layer may import only the layers below it.

```mermaid
flowchart TD
    api["api: routers, schemas, problem details"] --> services["services: use cases, transactions"]
    services --> repositories["repositories: table mappings and all SQL"]
    services --> domain["domain: entities and booking rules"]
    repositories --> domain
```

import-linter enforces the layers in CI: routers never import repositories, and the domain imports no framework. `core` holds cross-cutting infrastructure: configuration, password hashing and tokens.

```text
holdfast-booking-platform/
├── src/holdfast/
│   ├── main.py            # application factory and lifespan
│   ├── api/               # routers, request/response schemas, problem details
│   ├── services/          # use cases, one transaction each
│   ├── domain/            # entities and booking rules, free of frameworks
│   ├── repositories/      # table mappings and all SQL
│   └── core/              # settings, password hashing and tokens
├── migrations/            # Alembic environment and versioned migrations
├── scripts/
│   ├── seed.py            # realistic volume for measuring queries
│   └── explain.py         # EXPLAIN ANALYZE of the hot queries
├── tests/
│   ├── unit/              # domain rules, no database
│   ├── integration/       # through HTTP against a real Postgres
│   └── concurrency/       # many bookings racing for the same slot
├── compose.yaml
├── Dockerfile
└── Makefile
```

## Architecture decisions

Each structural decision is recorded once and never edited after acceptance; a later decision supersedes it instead.

<details>
<summary><strong>ADR-0001: Technology stack</strong> (Accepted, 2026-10-31)</summary>

**Context.** Holdfast is a backend-first learning and portfolio project. Effort should go into concepts (concurrency control, tenancy, distributed workflows), not into learning a language. The booking domain needs a database that can enforce non-overlapping time ranges natively.

**Options considered.**

1. *Python with FastAPI.* Familiar, async-native, generates OpenAPI docs for free. Weaker static guarantees than JVM or .NET, mitigated with strict mypy.
2. *Java with Spring Boot.* Industry standard in enterprise shops, but a new language and framework would dominate the learning effort.
3. *C# with ASP.NET Core.* Same trade-off as Spring Boot.

For the database, PostgreSQL was the only serious candidate: range types, exclusion constraints and `SKIP LOCKED` are central to later phases, and MySQL offers none of them.

**Decision.** Python 3.14 with FastAPI, PostgreSQL 18, SQLAlchemy 2.0 in async mode over asyncpg, Alembic for migrations, Pydantic v2 at the API edge, uv for dependency management, and pytest with Testcontainers so every integration test runs against a real Postgres.

**Consequences.** One async stack end to end, so the Phase 2 concurrency tests exercise real parallel requests. Tests need Docker, locally and in CI. PostgreSQL 18 is required (see ADR-0003). The stack is fixed for the life of the project.

</details>

<details>
<summary><strong>ADR-0002: Layered architecture and import rules</strong> (Accepted, 2026-10-31)</summary>

**Context.** The codebase will grow for months and gain background workers and extracted services. Business rules must stay testable without a web server or database, and tenant filtering must live in one predictable place.

**Options considered.**

1. *Ports and adapters.* The domain defines repository interfaces and repositories implement them. Maximum decoupling, but every repository needs an interface plus an implementation, and the integration tests already run against real Postgres, so fake repositories add little.
2. *Strict layers.* `api → services → repositories → domain`, each importing only downward. Less ceremony, same protection for the domain.
3. *No enforced layering.* Fastest to start, but structure erodes without a check.

**Decision.** Strict layers:

- `api` parses the request, calls exactly one service function and maps the result to a response.
- `services` implement use cases and own transactions and permission checks.
- `repositories` hold all SQL; every query on tenant data filters by `org_id`. SQLAlchemy mappings are named `*Record` and are translated into domain entities, never returned past this layer.
- `domain` holds entities, value objects and rules, and imports nothing from FastAPI or SQLAlchemy.
- `core` holds cross-cutting infrastructure and may be used by any layer.

The `/health` endpoint is infrastructure, not a use case, so it checks the database engine directly. The rules will be enforced in CI with import-linter once the `services` and `domain` packages exist in Phase 1.

**Consequences.** Domain rules are unit-testable in milliseconds and tenant filtering has a single home. Mapping between records and entities is extra code.

</details>

<details>
<summary><strong>ADR-0003: Primary keys are database-generated UUIDv7</strong> (Accepted, 2026-10-31)</summary>

**Context.** Every table needs a primary key, and IDs appear in URLs. Sequential integers expose how many bookings or organisations exist and invite guessing other tenants' IDs.

**Options considered.**

1. *`bigint` identity.* Compact and fast, but enumerable.
2. *UUIDv4.* Unguessable, but random values scatter B-tree inserts and bloat indexes.
3. *UUIDv7.* Unguessable to outsiders and time-ordered, so inserts stay at the right edge of the index.

**Decision.** UUIDv7 primary keys generated by PostgreSQL 18's built-in `uuidv7()` as the column default.

**Consequences.** Keys are 16 bytes instead of 8, and a key reveals its row's creation time, which is acceptable here. Clients never choose IDs. PostgreSQL 18 or newer is required in every environment.

</details>

<details>
<summary><strong>ADR-0004: Errors are RFC 9457 problem details</strong> (Accepted, 2026-10-31)</summary>

**Context.** Clients need one predictable shape for every failure: invalid input, missing entities, broken booking rules and server faults. FastAPI's defaults use one body for validation errors and another for everything else.

**Options considered.**

1. *FastAPI's defaults.* No code, but two shapes, and validation errors echo the rejected input back, which could leak secrets once passwords arrive.
2. *A custom envelope* such as `{"error": {"code": ..., "message": ...}}`. Simple, but every client has to learn a format that exists only here.
3. *RFC 9457 problem details.* An IETF standard that HTTP tooling already understands, with room for extension members such as `errors`.

**Decision.** Every error response is `application/problem+json`. Services raise domain errors, and one table in `api/errors.py` maps each to a status code. A problem that means more than its status code gets a type URI that resolves to its documentation in this README; the rest use `about:blank`. Validation problems point at each invalid input without echoing its value. Unexpected exceptions return a generic `500` problem while the traceback goes to the server log.

**Consequences.** One error format everywhere, and OpenAPI documents every `4XX` and `5XX` response as a problem. Type URIs are part of the API contract, so they must stay stable even if the documentation moves.

</details>

<details>
<summary><strong>ADR-0005: The organisation is part of the URL path</strong> (Accepted, 2026-10-31)</summary>

**Context.** Every request for tenant data must name its organisation, so that repositories can scope their queries. The project brief suggested passing it in a header.

**Options considered.**

1. *An `X-Organisation-ID` header.* Shorter URLs, but organisation CRUD would still use paths, giving two styles, and the tenant would not show in URLs or access logs.
2. *A path prefix, `/organisations/{organisation_id}`.* Explicit, uniform and visible in logs and links; the pattern GitHub uses for `/orgs/{org}`. URLs get longer.
3. *A subdomain per tenant.* Needs DNS and TLS for every tenant, which is far more than this project needs.

**Decision.** The path prefix, with shallow nesting: collections sit under their parent (`…/locations/{location_id}/resources`) and single items directly under the organisation (`…/resources/{resource_id}`). Every repository query on tenant data filters by `organisation_id`, and composite foreign keys such as `(organisation_id, location_id)` stop a row from ever referencing another tenant's row. Until Phase 3 adds authentication, an `X-User-ID` header names the user making a booking.

**Consequences.** A request for another tenant's data finds nothing and returns `404`. Phase 3 only has to check that the caller is a member of the organisation in the path.

</details>

<details>
<summary><strong>ADR-0006: Bookings are UTC ranges checked against local rules</strong> (Accepted, 2026-10-31)</summary>

**Context.** Bookings must never overlap, must respect each location's opening hours in its own time zone, and must stay correct across daylight-saving changes. One organisation can have locations in several time zones.

**Options considered.**

1. *Separate start and end columns.* Familiar, but every overlap check becomes hand-written comparisons, and the Phase 2 exclusion constraint needs a single range value.
2. *A `tstzrange` column in UTC.* Postgres understands range overlap (`&&`) natively and can index and constrain it.
3. *Local wall-clock times.* Simple to display, but ambiguous across daylight-saving changes and between time zones.

**Decision.** A booking's period is a half-open `[start, end)` `tstzrange` in UTC, so back-to-back bookings do not overlap. Each location has an IANA time zone and weekly opening hours in local wall-clock time, stored as a small JSONB value on the location because they are always read and replaced together. The booking rules are checked in local time, while durations and prices use elapsed UTC time.

**Consequences.** Overlap and availability queries are range operations, ready for an exclusion constraint. A booking that spans a daylight-saving change is charged for the hours that actually pass. Overnight and multi-day bookings are not possible.

</details>

<details>
<summary><strong>ADR-0007: Postgres prevents double-booking with an exclusion constraint</strong> (Accepted, 2026-11-05)</summary>

**Context.** Phase 1 checked for an overlapping booking and then inserted, so two requests could both pass the check before either committed. Measured with 50 simultaneous requests for one slot, that code confirmed two to four bookings for the same hour in each of five runs.

**Options considered.**

1. *Lock the resource row with `SELECT … FOR UPDATE`, then check and insert.* Correct only while every write path remembers to take the lock first, and it queues every booking of a resource, even bookings that cannot overlap.
2. *An advisory lock per resource.* The same queueing and the same reliance on discipline, with a lock key the schema knows nothing about.
3. *`SERIALIZABLE` isolation.* Postgres detects the conflict, but every booking path then needs a retry loop for serialization failures.
4. *An exclusion constraint.* `EXCLUDE USING gist (resource_id WITH =, period WITH &&)` makes the database reject any overlapping active booking, whichever code path writes it, while bookings that do not overlap proceed in parallel. Its index also serves the availability query.

**Decision.** The exclusion constraint, which needs the `btree_gist` extension. It covers every status except `cancelled`, so a status added later blocks overlaps until it is deliberately freed; forgetting to update the constraint can never allow a double booking. Bookings are written with `INSERT … ON CONFLICT ON CONSTRAINT ex_bookings_no_overlap DO NOTHING`, and an insert that returns no row becomes a `409`. A plain `INSERT` also keeps the guarantee, but colliding inserts wait for each other: in the concurrency suite's racing test it turned 38 to 57 of 190 losing requests into deadlock errors, each after a one-second wait, against none with `ON CONFLICT`.

**Consequences.** No double booking however many API instances run, which the concurrency suite proves on every CI run. Losing requests get a clean `409` at once. A new booking status needs a migration if it should free its slot. Every environment needs `btree_gist`, a trusted extension that managed Postgres services such as Neon provide.

</details>

<details>
<summary><strong>ADR-0008: Short-lived JWT access tokens with rotating refresh tokens</strong> (Accepted, 2026-11-08)</summary>

**Context.** Every request must prove who sent it, replacing the temporary `X-User-ID` header of ADR-0005. The API will run as several instances, its clients are the OpenAPI page today and a web app later, and a stolen credential must be possible to cut off.

**Options considered.**

1. *Server-side sessions.* An opaque token looked up on every request. Revocation is instant, but every call costs a database query.
2. *Long-lived JWTs.* No lookups at all, but a stolen token keeps working until it expires and cannot be revoked.
3. *Short-lived JWT access tokens plus rotating refresh tokens.* Access tokens are verified with the signing key alone. Refresh tokens are opaque, stored only as hashes, and replaced on every use; sending a used one again revokes every token from that sign-in.

**Decision.** Option 3. Access tokens last 15 minutes and are signed with HS256, the only algorithm the API accepts, so a forged `alg: none` token is refused. Refresh tokens last 30 days, and each sign-in's chain of them shares a family id so that a replay revokes all of them. Passwords are hashed with Argon2id in a worker thread, an unknown email takes as long to reject as a wrong password, and old hashes are upgraded at login when the hashing settings get stronger. Responses that carry tokens are marked `Cache-Control: no-store`.

**Consequences.** Authenticating a request needs no database lookup. Logging out or detecting a replay ends the refresh tokens at once, but an access token already issued stays valid for up to 15 minutes. Two clients that refresh with the same token at the same moment are treated as a replay and signed out, a deliberate trade for simplicity. Only the API holds the HS256 key, so if extracted services ever need to verify user tokens, the keys must become asymmetric. Expired refresh tokens pile up until a Phase 5 job removes them.

</details>

<details>
<summary><strong>ADR-0009: Roles belong to memberships, and services enforce them</strong> (Accepted, 2026-11-08)</summary>

**Context.** One user can belong to several organisations with a different role in each. Permission checks must live in the service layer, not only in routers, and tenant isolation must leak nothing to outsiders.

**Options considered.**

1. *One global role per user.* Simple, but it cannot express "admin here, member there".
2. *Checks in the routers, as FastAPI dependencies.* Visible in each route's signature, but background workers and any future entry point would bypass them.
3. *A role per membership, checked at the start of every service function.* Every entry point passes the same check.

**Decision.** Option 3. Every organisation-scoped use case starts with `require_membership`, which reports outsiders as `404`, exactly like an organisation that does not exist, and members below the required role as `403`. The rules about who may manage whom and who sees every booking live in the domain's `Membership`. Any change to memberships first locks the organisation's row, so two owners who step down at the same moment cannot leave it without an owner.

**Consequences.** A permission matrix calls every organisation endpoint as five kinds of caller, and a test fails when a new endpoint is missing from it. Every service function takes the caller's id. Outsiders cannot tell an organisation that exists from one that does not.

</details>

<details>
<summary><strong>ADR-0010: Lists page by keyset, never by offset</strong> (Accepted, 2026-11-10)</summary>

**Context.** Every list endpoint returns pages, and some lists grow without end: a resource gains bookings every day it exists. Phase 1 adopted opaque cursors; Phase 4 measured the alternative at depth.

**Options considered.**

1. *`OFFSET` and `LIMIT`.* Simple, and clients can jump to any page, but Postgres produces and discards every row before the page, so deep pages get slower. A row inserted between two requests also shifts every later page, repeating or skipping an item.
2. *Keyset pagination.* Each page asks for the rows after the last one seen, which an index finds directly, so every page costs the same and new rows shift nothing. There are no page numbers or totals.

**Decision.** Keyset pagination behind an opaque cursor that holds the last item's id. Lists are sorted by their UUIDv7 id, which is creation order. Your upcoming bookings are sorted by start time and then id; their cursor still holds only the id, and the booking's start is looked up from it. On the busiest resource, the last page took 9.09 ms with `OFFSET` and 0.31 ms with a cursor.

**Consequences.** A page costs the same however deep a client reads. Clients cannot jump to a page number or show a total count. Each list needs an index that matches its order, such as `(resource_id, id)` for a resource's bookings. Cursors are opaque, so their encoding can change without breaking clients.

</details>

<details>
<summary><strong>ADR-0011: Indexes follow measured queries and are built concurrently</strong> (Accepted, 2026-11-10)</summary>

**Context.** An index speeds up the reads that use it, but slows every write to its table and takes space. Which queries need one only shows at volume. The bookings table must keep taking writes while an index is being added.

**Options considered.**

1. *Index every foreign key and filtered column up front.* A simple rule, but it builds indexes that no query uses and misses those that need an expression or a condition, such as a booking's end time or only active bookings.
2. *Wait for slow queries in production.* Real evidence, but only after users have suffered for it.
3. *Measure plans on seeded volume and add what the numbers justify.* A seed builds a realistic million bookings, and a script compares each hot query's plan with and without the candidate indexes.

**Decision.** Option 3, through `make seed` and `make explain`, with the results recorded under Performance. Migrations build indexes with `CREATE INDEX CONCURRENTLY`, which does not block writes but cannot run inside a transaction.

**Consequences.** Every index has a measured reason, and a query that is already fast, such as availability, gets none. Each booking now writes two more index entries. A migration that builds indexes concurrently is not atomic: if a build fails, it leaves an invalid index that must be dropped before the migration runs again. The numbers need measuring again whenever the shape of the data changes.

</details>

## License

[MIT](LICENSE) © 2026 Francis Remoshan
