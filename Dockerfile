# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.14-slim
RUN useradd --system --no-create-home holdfast
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY pyproject.toml .
COPY migrations migrations
ENV PATH="/app/.venv/bin:$PATH"
USER holdfast
EXPOSE 8000
CMD ["uvicorn", "holdfast.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
