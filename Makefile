# Recipes are single commands so they run the same under sh (CI, Git Bash) and cmd.exe.
.PHONY: install up down migrate format lint typecheck test check

install:
	uv sync
	uv run pre-commit install

up:
	docker compose up --build --detach

down:
	docker compose down

migrate:
	docker compose run --build --rm migrate

format:
	uv run ruff format
	uv run ruff check --fix

lint:
	uv run ruff check
	uv run ruff format --check

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test
