# syntax=docker/dockerfile:1

# Builds the image of one service, named by SERVICE, such as bookings:
#   docker build --build-arg SERVICE=bookings .
# The image installs only that service and its dependencies. Compose sets each process's command.

FROM python:3.14-slim AS builder
ARG SERVICE
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace --package "holdfast-${SERVICE}"

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package "holdfast-${SERVICE}"


FROM python:3.14-slim
ARG SERVICE
RUN useradd --system --no-create-home holdfast
WORKDIR /app
COPY --from=builder /app/.venv .venv
# The service's settings, including where Alembic finds its migrations.
COPY services/${SERVICE}/pyproject.toml .
COPY services/${SERVICE}/migrations migrations
ENV PATH="/app/.venv/bin:$PATH"
USER holdfast
