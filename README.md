# agent-remote-server

<p align="center"><img src="assets/agent-remote-icon.svg" alt="Agent Remote icon" width="80" height="80"></p>

<p align="center">
  <a href="https://github.com/Agent-Remote/agent-remote-server/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Agent-Remote/agent-remote-server/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://codecov.io/gh/Agent-Remote/agent-remote-server"><img alt="Codecov" src="https://codecov.io/gh/Agent-Remote/agent-remote-server/graph/badge.svg"></a>
  <a href="https://github.com/Agent-Remote/agent-remote-server/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/Agent-Remote/agent-remote-server?style=flat&logo=github"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/Agent-Remote/agent-remote-server"></a>
</p>

English | [中文](README.zh-CN.md)

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

The runtime control plane also provides:

- Per-node runtime backend allowlists, defaults, policy, capability reporting, and backend-aware scheduling.
- Per-account runtime backend pinning and explicit migration between Native Runtime and Docker Sandbox.
- Session runtime identity, interrupted-session reconciliation, and replacement-session lineage without command replay.
- A narrow task contract between the unprivileged node worker and the privileged Native Runtime helper.
- Revisioned, device-scoped SSH key synchronization with attach readiness reporting.
- Guarded deletion for retired resources: dependencies and lifecycle state are checked before records are removed.
- Session-scoped port-forward grants with one-time Redis tokens, renewable leases, quotas, immediate resource revocation, lifecycle cleanup, and metadata-only audit events.

Port forwarding is available only when the selected node explicitly advertises the capability for the session backend. The current release supports Native Runtime sessions only; Docker Sandbox requests fail closed. Application traffic flows directly between the device and node and never traverses this control plane.

The web console can delete failed or paused sync sessions. Active local Mutagen sessions must be
paused on their owning device first, so the control plane does not silently orphan a running sync.

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
- `PORT_FORWARDING_ENABLED`
- `PORT_FORWARD_MIN_PORT` / `PORT_FORWARD_MAX_PORT`
- `PORT_FORWARD_MAX_PER_USER` / `PORT_FORWARD_MAX_PER_DEVICE` / `PORT_FORWARD_MAX_PER_SESSION`
- `PORT_FORWARD_MAX_STREAMS`
- `PORT_FORWARD_DEFAULT_TTL_SECONDS` / `PORT_FORWARD_MAX_TTL_SECONDS`
- `PORT_FORWARD_CONNECTION_TOKEN_TTL_SECONDS` / `PORT_FORWARD_LEASE_SECONDS`
- `PORT_FORWARD_CONTROL_PLANE_GRACE_SECONDS`
- `PORT_FORWARD_BYTES_PER_SECOND`
- `PORT_FORWARD_CLEANUP_INTERVAL_SECONDS`
- `PORT_FORWARD_CREATE_RATE_LIMIT_PER_MINUTE` / `PORT_FORWARD_REDEEM_RATE_LIMIT_PER_MINUTE`
- `DEVICE_CONTROL_ENABLED`
- `DEVICE_CONTROL_V2_ENABLED` (defaults to `true`; emergency rollback switch for new generations)
- `DEVICE_CONTROL_RELEASE_EVIDENCE_PATH`
- `DEVICE_CONTROL_RELEASE_PUBLIC_KEY`
- `DEVICE_SESSION_RETENTION_DAYS`
- `DEVICE_SESSION_AUDIT_RETENTION_DAYS`

See `.env.example`.

The manifest schema and canonical Ed25519 signing payload are documented in
`docs/device-control-release-evidence.md`.

Device control defaults to disabled. When `AGENT_REMOTE_ENV=production`, enabling it requires a
schema 8 release-evidence manifest shipped with the exact root distribution, bound to the exact
Server/component/artifact composition, and signed by the pinned Base64-encoded Ed25519 public key.
Schema 8 is permanently valid for that signed composition and has no `expires_at` field. Operators
must also choose explicit non-zero terminal-session
and device-session-audit retention periods; audit retention cannot be shorter than session
retention. Development may explicitly enable the capability only for non-sensitive test data.

Computer Use v2 is enabled for new generations by default. The Server negotiates v2 only when the
Node advertises the complete canonical capability set; missing, partial, malformed, or older sets
fall back atomically to v1. Set `DEVICE_CONTROL_V2_ENABLED=false` to force v1 for newly created
generations during an emergency. Active generations never change capability sets in place.

Schema 8 Apple and Community manifests may carry the optional artifact-bound v2 quality digest.
That digest supports release-quality auditing but is not runtime authorization. General signed
production release evidence remains mandatory whenever device control is enabled in production.

The user API exposes create/list/detail/reconnect/stop operations under `/api/v1/port-forwards`; the node API exposes redeem/renew/release operations. Connection tokens are returned once with `Cache-Control: no-store`, stored only as short-lived Redis values, and must never be logged or persisted by clients.

## Container

The Docker image runs Alembic migrations by default and then starts Uvicorn:

```sh
docker build -t agent-remote-server .
docker run --rm -p 8000:8000 \
  -e AGENT_REMOTE_SECRET_KEY=change-me \
  -e DATABASE_URL=postgresql+asyncpg://agent_remote:agent_remote@postgres:5432/agent_remote \
  -e REDIS_URL=redis://redis:6379/0 \
  agent-remote-server
```

Set `AGENT_REMOTE_RUN_MIGRATIONS=0` to skip migrations for one-off commands.

GitHub Actions builds and pushes the production image to GHCR for `v*` tags and creates a GitHub Release record with generated release notes.

## Current Boundary

This repository contains the control-plane API, persistence model, identity and device APIs, node/runtime policy, tool-account binding and migration state machines, session reconciliation, and node task polling APIs. Privileged isolation and process execution run in the node repository; local device networking and workspace synchronization run in the CLI repository.

## License

agent-remote-server is licensed under GPL-3.0-only. See `LICENSE`.

Third-party dependency notices are listed in `THIRD_PARTY_NOTICES.md`.
