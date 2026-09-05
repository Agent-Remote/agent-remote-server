# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM python:3.13-slim AS builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_HTTP_RETRIES=10
ENV UV_HTTP_TIMEOUT=60
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim

ARG AGENT_REMOTE_VERSION=0.2.12
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV PATH=/app/.venv/bin:$PATH
ENV AGENT_REMOTE_VERSION=$AGENT_REMOTE_VERSION

LABEL org.opencontainers.image.version=$AGENT_REMOTE_VERSION

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src
COPY alembic.ini README.md LICENSE ./
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN chmod +x ./scripts/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "agent_remote_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
