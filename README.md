# Holdfast

[![CI](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/remoshan/holdfast-booking-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A multi-tenant booking platform for shared spaces. Organisations list desks and meeting rooms, members reserve time slots, and the system guarantees no double-booking, even when fifty people click "Book" on the same room at the same moment.

The name is the promise: a reservation hold that holds fast.

## Guarantees

1. **No double-booking.** No two active bookings overlap on the same resource. The database enforces this, not just application code.
2. **Tenant isolation.** A user can never read or change another organisation's data. Cross-tenant requests return `404`, so they leak nothing.
3. **Holds expire.** A hold that is not paid for in time expires and frees the slot.

## Roadmap

Each phase ends with a deployable system, a merged pull request and its architecture decisions recorded below.

| Phase | Focus | Status |
| --- | --- | --- |
| 0 | Foundations: Compose, CI, migrations, health check | In progress |
| 1 | Core API: CRUD, availability search, problem+json errors | Planned |
| 2 | No double-booking: exclusion constraint, concurrency tests | Planned |
| 3 | Authentication and per-organisation roles | Planned |
| 4 | Data under pressure: indexes, query plans, pagination at depth | Planned |
| 5 | Background work: expiring holds, job queue, idempotency | Planned |
| 6 | Payment saga, outbox, two extracted services | Planned |
| 7 | Hardening: OWASP review, rate limits, tracing | Planned |

## Tech stack

| Concern | Choice |
| --- | --- |
| Language and framework | Python 3.14, FastAPI |
| Database | PostgreSQL 18 |
| Data access and migrations | SQLAlchemy 2.0 (async, asyncpg), Alembic |
| Settings and validation | Pydantic v2, pydantic-settings |
| Testing | pytest, HTTPX, Testcontainers (real Postgres, never SQLite) |
| Quality gates | Ruff, mypy (strict), pre-commit |
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

The API is then available at <http://localhost:8000>, with interactive OpenAPI docs at <http://localhost:8000/docs>. Stop everything with `make down`.

### Commands

| Command | What it does |
| --- | --- |
| `make install` | Install dependencies and the pre-commit hooks |
| `make up` / `make down` | Start or stop the full stack in Docker |
| `make migrate` | Apply new migrations to the running database |
| `make format` | Format code and apply safe lint fixes |
| `make lint` | Ruff lint and format check |
| `make typecheck` | mypy in strict mode |
| `make test` | Run the test suite (needs Docker for Testcontainers) |
| `make check` | Lint, type-check and test: exactly what CI runs |

### Configuration

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy URL with the asyncpg driver, e.g. `postgresql+asyncpg://user:password@host:5432/holdfast` |

## Architecture

Holdfast is a modular monolith with strict layers. Each layer may import only the layers below it.

```mermaid
flowchart TD
    api["api: routers and schemas"] --> services["services: use cases, transactions, permissions"]
    services --> repositories["repositories: SQL and persistence"]
    services --> domain["domain: entities and business rules"]
    repositories --> domain
```

`core` holds cross-cutting infrastructure (configuration) and is available to every layer. The `services` and `domain` layers arrive with the first use cases in Phase 1.

```text
holdfast-booking-platform/
├── src/holdfast/
│   ├── main.py            # application factory and lifespan
│   ├── api/               # routers and request/response schemas
│   ├── core/              # settings
│   └── repositories/      # table mappings and SQL
├── migrations/            # Alembic environment and versioned migrations
├── tests/integration/     # tests through HTTP against a real Postgres
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

## License

[MIT](LICENSE) © 2026 Francis Remoshan
