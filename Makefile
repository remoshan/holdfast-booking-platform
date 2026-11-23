# Recipes are single commands so they run the same under sh (CI, Git Bash) and cmd.exe.
# Each service checks its import rules, types and tests from its own folder, with its own
# settings, in the environment of the whole workspace: from a service's folder, uv would otherwise
# install only that service and leave out the shared development tools.
IN_BOOKINGS = uv run --all-packages --directory services/bookings

.PHONY: install up down migrate seed explain format imports lint typecheck test check

install:
	uv sync
	uv run pre-commit install

up:
	docker compose up --build --detach

down:
	docker compose down

migrate:
	docker compose run --build --rm migrate

seed:
	$(IN_BOOKINGS) python scripts/seed.py

explain:
	$(IN_BOOKINGS) python scripts/explain.py

format:
	uv run ruff format
	uv run ruff check --fix

imports:
	$(IN_BOOKINGS) lint-imports

lint: imports
	uv run ruff check
	uv run ruff format --check

typecheck:
	$(IN_BOOKINGS) mypy

test:
	$(IN_BOOKINGS) pytest

check: lint typecheck test
