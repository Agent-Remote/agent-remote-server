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

Port forwarding is available only when the selected node explicitly advertises the capability for the session backend. The current release supports both Native Runtime and Docker Sandbox sessions; backend-specific capability or trusted runtime-state failures still fail closed. Application traffic flows directly between the device and node and never traverses this control plane.

The web console can delete failed or paused sync sessions. Active local Mutagen sessions must be
paused on their owning device first, so the control plane does not silently orphan a running sync.

Managed Native startup has an authenticated result-confirmation endpoint bound to the original
snapshot, task record and lease attempt. Exact committed retries return the historical receipt
without reviving a stopped session or reacquiring retired skill content. Generic task-result routes
enforce the same managed authorization. Read-only inspection distinguishes a committed receipt from
an older unaccepted attempt without renewing authority. Native worker dispatch now uses this contract;
finalization transport, restart recovery and runtime acceptance remain required before advertising support.

Managed Native termination now has a separate exact snapshot endpoint and immutable receipt
(migration 0048). It fences unaccepted startup tasks, preserves accepted startup history and revokes
device/browser bindings atomically. The Node confirms termination before its first frozen-content
upload; stopped, persisted and published remain separate outcomes. See
[the termination contract](docs/skill-session-termination.md). This does not enable backend capability.

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
- `DEVICE_SESSION_AUTHORIZATION_MODE` (`per_application_approval` during compatibility rollout;
  set to `session_full_trust` only for a fully capable signed component combination)
- `DEVICE_CONTROL_RELEASE_EVIDENCE_PATH`
- `DEVICE_CONTROL_RELEASE_PUBLIC_KEY`
- `DEVICE_SESSION_RETENTION_DAYS`
- `DEVICE_SESSION_AUDIT_RETENTION_DAYS`

See `.env.example`.

The manifest schema and canonical Ed25519 signing payload are documented in
`docs/device-control-release-evidence.md`.

Device control defaults to disabled. When `AGENT_REMOTE_ENV=production`, current releases require a
schema 9 release-evidence manifest shipped with the exact root distribution, bound to the exact
Server/component/artifact composition, and signed by the pinned Base64-encoded Ed25519 public key.
Schema 9 is permanently valid for that signed composition and has no `expires_at` field. Already
issued schema 8 manifests remain permanently verifiable for their exact signed compositions, but
they authorize only the legacy `per_application_approval` policy; production `session_full_trust`
fails closed unless the verified manifest is schema 9. Operators
must also choose explicit non-zero terminal-session
and device-session-audit retention periods; audit retention cannot be shorter than session
retention. Development may explicitly enable the capability only for non-sensitive test data.

Computer Use v2 is enabled for new generations by default. The Server negotiates v2 only when the
Node advertises the complete required `observation_mode_v2`, `ax_state_v2`, and
`adaptive_settle_v2` base, then includes supported extensions such as
`clipboard_payload_v2`. Missing, partial, unknown, or malformed sets fall back atomically to v1.
Set `DEVICE_CONTROL_V2_ENABLED=false` to force v1 for newly created generations during an
emergency. Active generations never change capability sets in place.

Schema 9 Apple and Community manifests may carry the optional artifact-bound v2 quality digest.
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

Managed session stop responses expose `skill_finalization_operation_id` (the original snapshot UUID).
The owner can query `GET /api/v1/sessions/skill-finalizations/{operation_id}` with the existing user
or device session token, including after session deletion. Process confirmation, data persistence,
latest publication and content retention are reported separately. See [stop status](docs/skill-stop-status.md).

Native account takeover tasks now maintain their exact poll-attempt lease through Helper capture
and retained upload. Generic task completion requires the original committed initial checkpoint
and an exact bounded result; failure cannot consume a pending reservation. Durable Server and Helper
records recover retries without reimporting source changes. Ordinary Native session admission now
initiates reservations; mixed-backend acceptance remains pending; no managed capability is enabled by this integration.

First managed Native session creation now reserves takeover of the original account directory on
its original Node and returns MIGRATION_PENDING with a durable operation ID and explicit evidence
that no session was created. Retries reuse that reservation and leave legacy sessions running.
GET /api/v1/sessions/skill-takeovers/{operation_id} gives owner/device-authenticated read-only progress,
including when new managed admission is disabled. fclaude waits up to 60 seconds, then submits one
new session request only after the original initial checkpoint commits. Unknown source binding,
changed task evidence or unavailable backend cannot silently switch to an empty directory.

Deployment acceptance now records an independent attempt for each original account target. Internal
retry acceptance preserves the original plan, appends only explicitly selected transient failures and
keeps completed targets unchanged. Status and retention fail closed on inconsistent history; public
retry submission and receipt lookup are now available under the original operation’s `/retries`
subresource. Ordinary polling now schedules compatible pending targets. See [attempt contract](docs/skill-deployment-attempts.md).

Changed skill configuration now marks an older unfinished deployment operation `superseded` when an
unfinished affected target’s saved selection actually differs. The original status points to the
first replacing operation; late target completion cannot turn that old operation ready. Account pins,
unrelated changes, staging and completed targets do not trigger replacement by generation alone.
Active/conflicted targets keep their original content references until independently ended. This
configuration fence does not establish Node cancellation or Helper drain.

Internal deployment reservation now saves a complete account-directory input and binds it to the
exact original attempt and Node task. Retries reuse that input; every authorization rechecks the
lease, selection and state epochs. Active tasks keep their content even after a failed projection or
configuration supersession. Migration 0050 adds the binding without inventing session use or Node
readiness. Ordinary polling invokes this reservation; dedicated revocation/drain governs terminal
execution. No deployment capability is advertised by the Node yet. See
[deployment dispatch boundary](docs/skill-deployment-dispatch.md).

Dedicated Node deployment manifest/file/lease endpoints now serve the exact reserved complete input.
Every request pins the current poll attempt; file copies release database locks and reauthorize before
sending verified bytes. The matching Go client validates original plan and tree digests, exact owner
identity and lease bounds. Download availability is `prepared_input`, never execution readiness.

Dedicated deployment result confirmation now atomically saves the original Helper preparation
receipt, task success and target readiness. Exact replay and read-only inspection recover committed
results even after input retirement. Worker execution journals the proposal before confirmation;
generic task completion remains forbidden. See [result contract](docs/skill-deployment-results.md).

Deployment termination now persists a permanent original-attempt revocation before accepting the
Helper's separate drain receipt. Atomic confirmation ends only the original task/target; exact
historical replay survives input retirement and preserves successors. Migration 0051 adds the
immutable intent, and retry dispatch requires an accepted drain result. Worker orchestration durably
recovers each revocation/drain/confirmation phase. See [termination contract](docs/skill-deployment-termination.md).

Initial account takeover now resolves manual sources through a separate immutable deployment discovery
record. It preserves accepted configuration plans and binds added local sources to the original
account, takeover, first versions and directory epoch. Actual task input, retries, supersession and
retention use that saved resolution. See [deployment discovery](docs/skill-deployment-discovery.md).
New compatible Native targets start pending, including known compatible offline Nodes. Authenticated
polling schedules bounded original targets and resumes resolved conflicts; existing task bindings keep
their dedicated execution/drain lifecycle. See [scheduling](docs/skill-deployment-scheduling.md).
Full runtime acceptance remains pending; no Node capability is activated.

Frozen Native snapshot export uses the user-authorized `/api/v1/skills/state/node-exports/{snapshot_id}/authorize`
and original-Node `/api/v1/node/skill-state-exports/{snapshot_id}/verify` endpoints. The matching CLI
reads the already-frozen data directly through restricted SSH, so exhausted Server state quota does
not prevent recovery. Live user/device/key checks remain mandatory; authorization is not evidence of
local data availability or upload completion. See [the export contract](docs/skill-node-export.md).
