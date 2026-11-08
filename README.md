# Holdfast

[![CI](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A multi-tenant booking platform for shared spaces. Organisations list desks and meeting rooms, members reserve time slots, and the system guarantees no double-booking, even when fifty people click "Book" on the same room at the same moment.

The name is the promise: a reservation hold that holds fast.

## Guarantees

1. **No double-booking.** No two active bookings overlap on the same resource. A Postgres exclusion constraint enforces this, so it holds however many requests race for the same slot.
2. **Tenant isolation.** A user can never read or change another organisation's data. Every request carries a signed access token, every query is scoped to one organisation, and requests from outside it return `404`, so they leak nothing.
3. **Holds expire.** From Phase 5, a hold that is not paid for in time expires and frees the slot.

## Roadmap

Each phase ends with a deployable system, a merged pull request and its architecture decisions recorded below.

| Phase | Focus | Status |
| --- | --- | --- |
| 0 | Foundations: Compose, CI, migrations, health check | Done |
| 1 | Core API: CRUD, availability search, problem+json errors | Done |
| 2 | No double-booking: exclusion constraint, concurrency tests | Done |
| 3 | Authentication and per-organisation roles | Done |
| 4 | Data under pressure: indexes, query plans, pagination at depth | Planned |
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

### Commands

| Command | What it does |
| --- | --- |
| `make install` | Install dependencies and the pre-commit hooks |
| `make up` / `make down` | Start or stop the full stack in Docker |
| `make migrate` | Apply new migrations to the running database |
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
- **Pagination.** Lists return `{"items": [...], "next_cursor": "..."}`. Pass `next_cursor` back as `cursor` for the next page; it is `null` on the last page. `limit` is 1 to 100 and defaults to 20.

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
| `tests/integration` | Every endpoint through HTTP against a real Postgres, both when the request succeeds and when it fails. A permission matrix calls every organisation endpoint as an anonymous caller, an outsider, a member, an admin and an owner, and fails if a new endpoint is missing from it. |
| `tests/concurrency` | That exactly one of 50 simultaneous requests for a slot wins, and that 20 inserts released at the same instant end in one booking and clean conflicts, never deadlocks. |

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

## License

[MIT](LICENSE) © 2026 Francis Remoshan
