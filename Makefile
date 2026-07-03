.PHONY: install test lint format typecheck run compose-up compose-down migrate

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy

run:
	uv run uvicorn agent_remote_server.main:app --reload

compose-up:
	docker compose up --build

compose-down:
	docker compose down

migrate:
	uv run alembic upgrade head

