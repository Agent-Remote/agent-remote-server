# agent-remote-server

Python control-plane API for agent-remote.

The repository currently provides the control-plane server foundation:

- FastAPI application factory.
- Settings loaded from environment and `.env`.
- Structured JSON logging.
- Request ID middleware.
- `/healthz` process health check.
- `/readyz` PostgreSQL and Redis readiness check.
- SQLAlchemy async engine helpers.
- Alembic initialization.
- Dockerfile and local Compose development stack.
- Basic tests.

## Requirements

- Python 3.13
- uv
- Docker and Docker Compose for local dependency services

## Local Setup

```sh
uv sync
cp .env.example .env
```

Run tests:

```sh
uv run pytest
```

Run API locally:

```sh
uv run uvicorn agent_remote_server.main:app --reload
```

Run local Compose stack:

```sh
docker compose up --build
```

Health checks:

```sh
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

## Configuration

Environment variables:

- `AGENT_REMOTE_ENV`
- `AGENT_REMOTE_SECRET_KEY`
- `PUBLIC_BASE_URL`
- `DATABASE_URL`
- `REDIS_URL`
- `LOG_LEVEL`

See `.env.example`.

## Current Boundary

This repository skeleton does not yet implement business tables, auth, node task behavior, or user APIs. Those features must be added through explicit protocol and architecture updates.
