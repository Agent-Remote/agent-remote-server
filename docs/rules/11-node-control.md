# 11 Node Control

## Node Credentials

- Node registration tokens are one-time credentials.
- Node registration tokens and node tokens must be stored only as keyed hashes.
- Node credentials must not authenticate user-facing APIs.
- User bearer tokens must not authenticate node APIs.

## Heartbeats

- Nodes actively submit heartbeats to the control plane.
- Heartbeat payloads may include version, supported tool types, resource counters, and runtime capability flags.
- Heartbeats must not include private keys, tool login state, cookies, browser contents, or shell command output.
- Stale heartbeat detection should mark nodes offline without deleting node records.

## Task Leases

- Nodes poll for tasks; nodes do not expose public HTTP APIs.
- Polling leases only tasks owned by the authenticated node.
- A leased task must include a lease deadline.
- Expired leases can be reissued while the task is not terminal.
- Terminal statuses are `succeeded`, `failed`, `cancelled`, and `expired`.

## Task Results

- Task completion and failure reporting must be idempotent by `task_id`.
- Repeated completion calls for the same `task_id` must not create duplicate result rows.
- Task result payloads must not contain secrets, cookies, private keys, or tool login state.

## Reconciliation

- Reconciliation snapshots are node-owned status summaries.
- Store section names and summary keys in audit logs, not full sensitive state.
- Runtime session summaries contain only session IDs, backend names, neutral resource IDs, and active flags.
- A node startup reconciliation may mark missing native sessions `interrupted`; it must not request command replay.

## Runtime Tasks

- Account binding, session lifecycle, workspace ownership, Docker access, and native isolation are executed through the node's privileged runtime helper.
- Runtime task payloads carry IDs, locale, timezone, bounded policy, and declared backend. They must not carry host-derived paths outside managed roots.
- Backend migration requires no active sessions and a live target capability. Task failure preserves the original pinned backend.
