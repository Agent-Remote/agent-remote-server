# 03 Tech Stack

## Runtime

- Python 3.13 only.
- FastAPI for HTTP API.
- Pydantic v2 and `pydantic-settings` for schemas and settings.
- SQLAlchemy 2 async engine.
- Alembic for migrations.
- Argon2id through `argon2-cffi` for password hashing.
- `cryptography` for reversible encryption of server-side authentication secrets.
- `redis.asyncio` for Redis.
- Uvicorn for ASGI serving.

## Development Tools

- `uv` owns Python dependency resolution and lockfile generation.
- Ruff owns formatting and linting.
- Mypy owns static type checking.
- Pytest owns tests.
- Docker Compose owns the local development stack.

## Dependency Rules

- Runtime dependencies must be declared in `[project].dependencies`.
- Developer-only tools must be declared in `[dependency-groups].dev`.
- Do not add a dependency for a small helper that can be implemented clearly in the standard library.
- New dependencies require a short justification in the pull request.

## Python Version Policy

`pyproject.toml`, Dockerfile, and CI must all target Python 3.13.
