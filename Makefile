# Recipes are single commands so they run the same under sh (CI, Git Bash) and cmd.exe.
# Each member checks its import rules, types and tests from its own folder, with its own
# settings, in the environment of the whole workspace: from a member's folder, uv would otherwise
# install only that member and leave out the shared development tools.
IN_BOOKINGS = uv run --all-packages --directory services/bookings
IN_NOTIFICATIONS = uv run --all-packages --directory services/notifications
IN_PAYMENTS = uv run --all-packages --directory services/payments
IN_MESSAGING = uv run --all-packages --directory libs/messaging

.PHONY: install up down migrate seed explain format imports lint typecheck test check

install:
	uv sync
	uv run pre-commit install

# Orphans are containers of services that were renamed or removed: left running, they would
# keep their ports and block the services that replaced them.
up:
	docker compose up --build --detach --remove-orphans

down:
	docker compose down --remove-orphans

migrate:
	docker compose run --build --rm bookings-migrate
	docker compose run --build --rm notifications-migrate
	docker compose run --build --rm payments-migrate

seed:
	$(IN_BOOKINGS) python scripts/seed.py

explain:
	$(IN_BOOKINGS) python scripts/explain.py

format:
	uv run ruff format
	uv run ruff check --fix

imports:
	$(IN_MESSAGING) lint-imports
	$(IN_BOOKINGS) lint-imports
	$(IN_NOTIFICATIONS) lint-imports
	$(IN_PAYMENTS) lint-imports

lint: imports
	uv run ruff check
	uv run ruff format --check

typecheck:
	$(IN_MESSAGING) mypy
	$(IN_BOOKINGS) mypy
	$(IN_NOTIFICATIONS) mypy
	$(IN_PAYMENTS) mypy

test:
	$(IN_MESSAGING) pytest
	$(IN_BOOKINGS) pytest
	$(IN_NOTIFICATIONS) pytest
	$(IN_PAYMENTS) pytest

check: lint typecheck test
