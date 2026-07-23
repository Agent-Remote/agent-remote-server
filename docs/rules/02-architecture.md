# 02 Architecture

## Module Layout

```text
src/agent_remote_server/
  api/          FastAPI route modules and dependencies
  middleware/   ASGI middleware
  models/       SQLAlchemy ORM models split by business domain
  repositories/ Database access helpers
  schemas/      Pydantic response and request models
  security/     Password, token, encryption, and TOTP helpers
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
- `services/` may depend on `security/` helpers for explicit security operations.
- `repositories/` may depend on `models/` and `db`.
- `security/` must not depend on API, database, repositories, services, Redis, or middleware modules.
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

## Runtime Backend Control

- The control plane owns backend policy; clients cannot select a backend when creating a session.
- A tool account pins `docker_sandbox` or `native` when it first binds. Sessions inherit that value.
- Backend changes use an explicit node task and commit only after node-side verification succeeds.
- Nodes report independently probed backend capabilities. Scheduling uses the intersection of the administrator allowlist and the reported capabilities.
- Missing native resources reported during reconciliation move active sessions to `interrupted`; the control plane never replays their commands.
- SSH forced commands use a stable device gateway. Attach and sync access are re-authorized against the control plane on every connection.

## Device WireGuard Enrollment

- A device-scoped token may create or update only its own active WireGuard peer.
- The control plane accepts and stores only the device public key; private key generation and storage remain local to the CLI.
- Re-enrollment keeps the existing peer ID and interface address so local repair does not change routing unexpectedly.
- WireGuard public key bodies must not be written to audit details or logs.
