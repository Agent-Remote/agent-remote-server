# 11 Node Control

## Node Credentials

Exact session attachment requires the configured installed gateway's session_attach v1 capability.
The new exact verify route checks and echoes node, session, device and actual SSH key identities,
and admits only Claude sessions. Legacy verification remains a separate compatible route.

- Node registration tokens are one-time credentials.
- Node registration tokens and node tokens must be stored only as keyed hashes.
- Node credentials must not authenticate user-facing APIs.
- User bearer tokens must not authenticate node APIs.

## Heartbeats

Normalize `session_lifecycle` by the [per-backend lifecycle contract](../session-lifecycle.md).
Only exact boolean/integer v1 entries survive; malformed or missing entries revoke
previous support. Worker version and historical task success are not substitutes
for the installed Helper's current probe.

- Nodes actively submit heartbeats to the control plane.
- Heartbeat payloads may include version, supported tool types, resource counters, and runtime capability flags.
- Heartbeats must not include private keys, tool login state, cookies, browser contents, or shell command output.
- Stale heartbeat detection should mark nodes offline without deleting node records.

## Task Leases

- Poll envelopes expose both the existing logical `task_id` and additive `task_record_id` UUID.
  Start/result/ledger operations keep the logical identity; skill content authorization uses the
  exact database UUID. Clients must never derive one from the other or substitute a payload field.

- Nodes poll for tasks; nodes do not expose public HTTP APIs.
- Polling leases only tasks owned by the authenticated node.
- PostgreSQL polling locks candidate rows with `SKIP LOCKED`, so concurrent polling or a renewal
  transaction cannot overwrite a lease selected from stale state. Task-result identity is unchanged.
- A leased task must include a lease deadline.
- Expired `leased` and `running` tasks can be reissued while the task is not terminal. Nodes must
  replay a locally persisted terminal result instead of repeating the runtime operation.
- Terminal statuses are `succeeded`, `failed`, `cancelled`, and `expired`.

## Task Results

- Binding/verification results preserve disabled account admission, including delayed
  success, failure and replay. Account row locks serialize these publications with disable.

- Task completion and failure reporting must be idempotent by `task_id`.
- Repeated completion calls for the same `task_id` must not create duplicate result rows.
- Task result payloads must not contain secrets, cookies, private keys, or tool login state.
- `cancel_ego_browser_request` persistence accepts only its exact three-field, boolean completion
  result. Malformed completions and all failure bodies are replaced with fixed content-free values
  before storage and cancellation reconciliation.

## Reconciliation

- Reconciliation snapshots are node-owned status summaries.
- Store section names and summary keys in audit logs, not full sensitive state.
- Runtime session summaries contain only session IDs, backend names, neutral resource IDs, and active flags.
- Legacy reconciliation marks inactive native sessions `interrupted` and may enqueue idempotent runtime cleanup without changing that user-visible status; it must not request command replay.
- Legacy sessions in `starting` are excluded even if the snapshot reports an inactive resource or
  process exit. Only the creation task's success or failure determines their startup outcome;
  reconciliation resumes after they become `running` or `active`.
- When reconciliation marks a tool session interrupted or confirms process exit, it also revokes
  the session's live device-control binding in the same database transaction.

## Runtime Tasks

- Account binding, session lifecycle, workspace ownership, Docker access, and native isolation are executed through the node's privileged runtime helper.
- Runtime task payloads carry IDs, locale, timezone, bounded policy, and declared backend. They must not carry host-derived paths outside managed roots.
- Backend migration requires no active sessions and a live target capability. Task failure preserves
  the original pinned backend, keeps admission closed and records recovery_required. It cannot prove
  source permissions were restored or authorize another migration. Results bind the original profile
  and task and remain immutable on replay, including after a later migration starts.
- Device-control activate, context-update, and deactivate tasks carry the complete binding.
  Delayed deactivation is a successful no-op when the active context belongs to a different
  `device_session_id` or newer generation; it must never stop the replacement bridge.

## Ego Browser Broker

- The authenticated Node daemon is the only remote component allowed to redeem a Node-role
  ego-browser relay ticket. The wrapper uses an owner-only Unix socket and cannot open the relay or
  declare user, device, tool-session, node, binding, or generation identity.
- Heartbeat capability metadata pins the wrapper and protocol versions. Unknown, stale, malformed,
  or incompatible metadata prevents claim instead of falling back to a remote browser or GUI path.
- The broker allocates monotonic sequences and one-time permits, keeps tickets and sealing keys in
  bounded memory, serializes relay writes, and dispatches out-of-order responses by the complete
  binding/generation/request/sequence tuple.
- The broker derives `agent-remote:<tool_session_id>` from its authenticated session context and
  puts that exact Task Space label in each permit. The wrapper cannot replace it with an environment
  value. The Server independently derives the same label when the binding is claimed, and the local
  Bridge rejects a request label or Task Space scope that differs from its persisted binding.
- Renewal carries the persisted allowlist revision and learning-bundle digest. Binding revocation,
  policy drift, generation change, broker restart, or relay failure clears pending permits and
  fails all affected callers without replaying heredoc scripts.

## Skill Manager

Read [Skill manager](../skill-manager.md) and its mandatory shared contract before changing Skill code.
Preserve user-scoped authorization, exact task/lease/snapshot bindings, atomic publication, reference
and retention transactions, and separate configuration/deployment/persistence receipts. Schema and
protocol changes require matching migrations, tests and callers; do not add stage-by-stage logs here.

## Native Temporary Storage Policy

Node create and update requests validate optional `runtime_policy.temporary_storage` as `disk`
or `tmpfs`, and `temporary_size_bytes` as an integral JSON number from 64 MiB through 16 GiB.
Invalid types, booleans and out-of-range sizes return HTTP 422 before persistence. Existing
policy keys and requests omitting these additive fields remain compatible; no database migration
or server-side default injection is required. New Native sessions use the Node's 16 GiB disk
filesystem default, while saved legacy session specs retain tmpfs. These fields do not change
Docker Sandbox storage. The privileged Node independently enforces its local resource limits.

Exact binding attach capability is derived from the configured installed gateway,
not the Node version. Both attachment admission and original-task polling require
`binding_attach.supported` literal true and `protocol_version` integer 1.
Malformed binding gateway reports clear the previous capability, including JSON
type changes that compare equal under Python (true, 1, and 1.0).

Workspace preparation results conditionally update only a matching starting sync
session; delayed results cannot override pause or another target binding.

Reviewed runtime recovery requests preserve the existing Node recovery binding v1/v2/v3, task
identity and lease/result protocol. Inspection is safe observation only. A matching confirmed
projection does not replace current admission or Node authorization. No new execution protocol
or automatic retry is introduced; see [request contract](../runtime-recovery-requests.md).

Legacy create/stop session task result retries must match the original task row, terminal outcome
and exact normalized JSON result. Replay cannot reapply resource/session/revocation updates.
Late first create results retain their task receipt without overriding a later session lifecycle.
Managed startup/stop and runtime recovery retain their existing specialized result authorization.

Legacy explicit stop failure keeps the session stopping; automatic cleanup keeps interrupted.
History deletion must preflight all associated stop tasks and original success receipts under
history, session, then task locks. Unresolved or inconsistent stops block single and bulk deletion
before reference release. Preserve create-failure deletion without stop tasks, immutable historical
receipts and managed retention checks; these safeguards are not new physical termination evidence.
