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

`agent-remote-server` is the FastAPI control plane for Agent Remote. It stores users, devices, tool accounts, workspaces, sessions, synchronization metadata, port-forward records and audit data, and exposes the APIs used by the CLI, Node and administration console.

## Current boundary

The server authorizes and records control-plane operations. Node executes approved runtime work, while the CLI owns local credentials, WireGuard, SSH, Mutagen and local operation journals. The server does not receive private keys or become a replacement for the local CLI.

Current API areas include authentication and device enrollment, account binding, workspace/sync preparation, exact session attach/create/stop/delete requests, port forwarding, runtime recovery requests, attachment-transfer metadata and the device-control APIs. Each consequential operation is bound to the relevant user/device/session/node/generation context and returns a structured result or an explicitly unknown outcome.

## Requirements

- Python 3.13
- `uv`
- Docker and Docker Compose for local services

## Local development

```sh
uv sync
cp .env.example .env
uv run pytest
uv run uvicorn agent_remote_server.main:app --reload
```

Run the local stack with:

```sh
docker compose up --build
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

The repository quality gate is `scripts/run-quality-checks.sh`; it runs formatting, linting, typing, docstring checks and tests.

## Container installation

The Docker image runs Alembic migrations by default and then starts Uvicorn:

```sh
docker build -t agent-remote-server .
docker run --rm -p 8000:8000 \
  -e AGENT_REMOTE_SECRET_KEY=change-me \
  -e DATABASE_URL=postgresql+asyncpg://agent_remote:agent_remote@postgres:5432/agent_remote \
  -e REDIS_URL=redis://redis:6379/0 \
  agent-remote-server
```

Set `AGENT_REMOTE_RUN_MIGRATIONS=0` to skip migrations for a one-off command.

## Configuration

Runtime configuration is read from `.env`/environment variables. The important groups are:

- `AGENT_REMOTE_ENV`, `AGENT_REMOTE_SECRET_KEY`, `PUBLIC_BASE_URL`
- `DATABASE_URL`, `REDIS_URL`, `LOG_LEVEL`
- Port-forward policy and rate limits: `PORT_FORWARD_*`
- Device-control policy and evidence: `DEVICE_CONTROL_*`
- Session retention and audit retention: `DEVICE_SESSION_*`

See `.env.example` and the current rule documents before changing a production setting. Device-control is fail-closed in production unless the exact signed release evidence and policy prerequisites are present.

## Deployment

The container entrypoint runs migrations before Uvicorn unless `AGENT_REMOTE_RUN_MIGRATIONS=0` is set. Production deployments must use persistent PostgreSQL/Redis data, owner-managed secrets and non-zero retention settings required by the operations policy. Release images are built from `v*` tags and published through the repository workflow.

## Script entry points

The repository keeps these release and verification entry points:

```sh
# Run formatting, linting, typing, docstring checks and tests.
scripts/install-githooks.sh
scripts/check_docstrings.py
scripts/run-quality-checks.sh
# Prepare a versioned server release and changelog metadata.
scripts/prepare-release.sh
# Generate reviewed device-control release evidence from a disposable fixture.
uv run python scripts/create_device_control_release_evidence.py --help
```

The Docker image uses `scripts/docker-entrypoint.sh` to apply migrations before
starting Uvicorn. There is no standalone production installer; use the Docker
Compose or container commands above. Run release and evidence scripts only with
disposable data and review their output before publishing.

## Documentation

- Architecture and boundaries: `docs/rules/01-project-overview.md`, `docs/rules/02-architecture.md`
- Data and identity: `docs/rules/09-data-persistence.md`, `docs/rules/10-identity-auth.md`
- Node control: `docs/rules/11-node-control.md`
- Current session/account contracts: `docs/session-lifecycle.md`, `docs/session-exact-attach.md`, `docs/session-creation-caller-key.md`, `docs/session-deletion-caller-key.md`
- Sync, import and recovery: `docs/workspace-sync-safety.md`, `docs/account-config-import-requests.md`, `docs/runtime-recovery-requests.md`
- Port forwarding and device enrollment: `docs/port-forward-caller-key.md`, `docs/device-registration-idempotency.md`
- Skills and device-control operations: `docs/skill-manager.md`, `docs/device-control-release-evidence.md`

These documents describe current contracts. Migrations, schemas and tests are the final compatibility authority.

## License

GPL-3.0-only. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
