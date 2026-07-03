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
UV_CACHE_DIR=/Users/rem/Documents/Git/agent-remote-server/.uv-cache uv run pytest
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

