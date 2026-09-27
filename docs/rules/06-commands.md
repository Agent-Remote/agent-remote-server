# 06 Commands

Use these commands for local development.

## Setup

```sh
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv sync
```

## Run

```sh
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run uvicorn agent_remote_server.main:app --reload
```

## Quality Gate

```sh
scripts/run-quality-checks.sh
```

Expanded commands:

```sh
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run ruff format --check .
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run ruff check .
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run mypy
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run pytest --cov=agent_remote_server --cov-report=term --cov-fail-under=70
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run python scripts/check_docstrings.py
```

## Hooks

```sh
scripts/install-githooks.sh
```

## Alembic

```sh
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run alembic heads
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run alembic upgrade head
```

## Docker

```sh
docker compose config
docker compose build server
docker compose up --build
```

Pytest uses the available CPU count, capped at eight workers, with `loadgroup` scheduling. Each Skill SQLite test receives
its own copy of a session-scoped empty schema; connections, rows, transactions, and foreign-key
checks remain isolated. Shared PostgreSQL/Redis integration modules are assigned to one worker
in `tests/conftest.py`; keep new external-service tests in that scheduling group. Setting
`SKILL_TEST_DATABASE_URL` serializes the complete suite on one worker. Use `uv run pytest -n 0`
for a serial diagnostic run, or `-n 2` on memory-constrained machines. Coverage still combines
all workers and enforces the same 70% threshold.

Coverage uses Python 3.13 system monitoring (`sysmon`) to reduce tracing overhead; the
source scope and coverage threshold are unchanged. Set `COVERAGE_CORE=ctrace` to compare
against the traditional tracer when diagnosing coverage behavior.
