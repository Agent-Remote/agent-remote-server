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
- Inactive native resources reported during reconciliation move active sessions to `interrupted`; process exits may enqueue idempotent runtime cleanup without replacing that status, and the control plane never replays their commands.
- Users may delete only `stopped` or `interrupted` tool sessions. Collection deletion removes all sessions in those two states for the current user; active lifecycle states remain protected and all deletion paths are audited.
- SSH forced commands use a stable device gateway. Attach and sync access are re-authorized against the control plane on every connection.
- SSH agent forwarding is authorized only for active developer credential profiles that explicitly select `agent_forwarding`; both the client attach response and the node forced-command verification carry that decision.

## Device WireGuard Enrollment

- A device-scoped token may create or update only its own active WireGuard peer.
- The control plane accepts and stores only the device public key; private key generation and storage remain local to the CLI.
- Re-enrollment keeps the existing peer ID and interface address so local repair does not change routing unexpectedly.
- WireGuard public key bodies must not be written to audit details or logs.

## Local Device Control Sessions

- `device_sessions` binds one macOS device to exactly one user-owned remote tool session and
  its assigned node. The client cannot override any member of that binding.
- A user token may request or stop control. Only the bound device token may report the local
  connection, submit local application approval, acquire the machine lock, renew the lease,
  reconnect, or invoke device-side stop.
- Reconnect and current-action abort increment the generation and clear the old lease while
  retaining an acquired machine lock. Only explicit session end, confirmed remote-session
  failure, or lease expiry releases the lock. Stop increments the generation and clears both.
- Generation is a positive signed 64-bit value. Non-terminal sessions are capped at
  `9223372036854775806`, reserving `9223372036854775807` for a final stop transition; an
  exhausted generation is rejected before state, audit, or Node-task mutation.
- The control plane stores lifecycle and audit metadata only. Application approvals contain a
  SHA-256 stable-identifier digest, control level, result, and clipboard boolean; GUI content,
  input, coordinates, titles, images, certificates, and plaintext relay payloads are forbidden.
- Relay and one-time connection material are separate short-lived infrastructure concerns and
  must not be added to the business session row.
- The bound device and assigned node register one ephemeral SPKI digest per generation. Redis
  exchanges each role's opposite-peer pin, shared 256-bit exporter context, and one-time relay
  ticket exactly once; none of these values enter SQL or structured logs.
- The device relay consumes role-bound tickets atomically, pairs only opposite roles with the
  same complete session binding and generation, accepts binary frames only, and enforces explicit
  per-frame, per-direction byte-rate, peer-wait, and connection-lifetime limits. It never parses
  or persists the nested TLS byte stream.
- Creation requires the assigned node's latest independent capability report to explicitly
  support protocol version 1, `platform=macos`, and the tool session's pinned runtime backend.
  A missing, stale, malformed, or incompatible report is denied rather than inferred.
- Creation and every generation change enqueue an idempotent `activate_device_control` task for
  the bound node. Terminal stop enqueues `deactivate_device_control`; these task payloads contain
  only binding and runtime location metadata, never relay tickets, keys, pins, or exporter data.
- A tool session records `device_control_protocol_version` only when its original Node creation
  task included the managed MCP configuration. Device-control creation requires this persisted
  fact and never infers injection from a later heartbeat.
- `device_control_enabled` defaults to false. Deployment operators may enable it only after the
  selected release profile's fixed gates pass. `apple-developer-id` requires signing,
  notarization, outbound allowlist, compatibility, and security review. The reduced
  `community-local-trust` profile requires explicit risk acceptance, project self-signing,
  official-runner automation, manual installation trust, and application-enforced egress.
  Production startup additionally requires a non-expired Ed25519-signed release-evidence manifest
  bound to the exact server version. The manifest pins the server, Node, application, proxy, SBOM, provenance,
  security-test, review or risk-acceptance, signing, outbound-policy, local-Claude-isolation,
  stop/revocation, and compatibility evidence digests. Schema 2 additionally pins community
  signing, official-runner automation, and risk-acceptance digests. Development may explicitly enable the
  capability without this production-only evidence gate for non-sensitive test data.
- Production device control requires explicit non-zero retention periods for terminal device-session
  metadata and device-session audit metadata. A bounded background service deletes only terminal
  sessions older than the configured stop-time cutoff and audit rows whose target type is
  `device_session`; it never deletes active sessions or general identity audit records.
