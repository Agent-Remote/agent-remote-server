# 02 Architecture

## Module Layout

```text
src/agent_remote_server/
  api/          FastAPI route modules and dependencies
  middleware/   ASGI middleware
  models/       SQLAlchemy ORM models split by business domain
  repositories/ Database access helpers
  schemas/      Pydantic response and request models
  services/     Application service helpers
  config.py     Environment-driven settings
  context.py    Request-local context
  db.py         SQLAlchemy engine and database helpers
  logging.py    Structured logging
  main.py       FastAPI application factory
  redis_client.py
```

## Dependency Direction

- `main.py` wires application components.
- `api/` may depend on `schemas/`, `config`, `db`, and `redis_client`.
- `services/` may depend on `repositories/`, `models/`, and `schemas/`.
- `repositories/` may depend on `models/` and `db`.
- `models/` may depend on `db` for the declarative base.
- `schemas/` must not import API, database, Redis, or middleware modules.
- `middleware/` may depend on `context` and standard logging only.
- `db.py` must not import API route modules.
- `redis_client.py` must not import API route modules.

## Application Factory

Use `create_app(settings: Settings | None = None)` for testability. Tests should inject explicit settings instead of mutating global environment whenever practical.

## Health Checks

- `/healthz` checks process-level health and must not depend on external services.
- `/readyz` checks dependencies required to serve traffic.
- Dependency failures must return a structured degraded response instead of crashing the process.
