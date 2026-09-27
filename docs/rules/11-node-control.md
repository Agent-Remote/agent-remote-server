# 11 Node Control

The generic `runtime_sessions` reconciliation list is not managed runtime termination authority.
Exclude any session with a persisted skill snapshot before missing/inactive observations can mutate
status, revoke connections or schedule legacy cleanup. In particular, an empty observation made
while a managed create task is pending/leased must leave that original startup admissible. Match
the snapshot's stable `session_reference_id`, not a task payload marker or feature flag. Original
managed start/termination/stop routes remain responsible for advancing those sessions.

Only the original authenticated Node can request finalization reclamation authorization; active
owner and terminal original session checks are repeated under the storage lock. The request selects
only a finalization UUID, never a user, path or substitute digest. Response bindings include original
Node/user/account/session/snapshot, incoming checkpoint/tree, unclean classification and current
terminal publication. Its 60-second observation starts after full content verification. Node callers
must validate all identities and a conservative elapsed-time budget, without turning an expired or
unknown reply into a deletion grant. Capability advertisement and content deletion remain separate.

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

- Before taking task locks, polling may schedule bounded original-Node deployment candidates under
  independent owner transactions, as specified in `docs/skill-deployment-scheduling.md`. Existing
  deployment bindings remain exclusively governed by their leased execution and drain protocols.

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
- Takeover renewal requires the exact task-record UUID and current `lease_attempt` from polling.
  It rechecks active ownership, account/directory binding and a live lease before extending the
  existing task. Each renewal grants at most 300 seconds, according to the positive configured
  task lease duration. Capture/file network waits do not hold user/task locks; mutation reauthorizes.

## Task Results

- Managed Native create-task outcomes must bind the original snapshot, task-record UUID, poll
  attempt, session/account/backend and canonical unit. Only exact bounded ready/stopped schemas
  are accepted. Initial publication requires the original reserved snapshot, starting session,
  active owner and unexpired current attempt under user/session/task locks. Terminal replay must
  match the saved original outcome and must not reapply it to a later session state. Generic
  completion/failure routes cannot bypass this check; legacy task behavior remains separate.

- Task completion and failure reporting must be idempotent by `task_id`.
- Repeated completion calls for the same `task_id` must not create duplicate result rows.
- `/node-api/tasks/{task_id}/managed-start-result` returns a committed skill envelope echoing the
  exact accepted ready/stopped payload. It requires managed API enablement and an actual managed task;
  it cannot turn a legacy task into a managed receipt. Generic `/complete` and `/fail` enforce the
  same managed authority guard, but do not provide the typed confirmation envelope.
- `POST /node-api/tasks/{task_id}/managed-start-result/inspect` performs a read-only inspection of
  one exact proposed outcome under the same original authority locks. It returns that outcome,
  whether its immutable receipt exists, and the current task status/poll attempt. It never renews a
  lease, changes snapshot/session state or authorizes launch. An absent receipt observed after the
  poll attempt has advanced proves that the older attempt cannot subsequently publish; an absence
  at the same attempt is only an observation, not permission to replace its pending outcome.
- Task result payloads must not contain secrets, cookies, private keys, or tool login state.
- `cancel_ego_browser_request` persistence accepts only its exact three-field, boolean completion
  result. Malformed completions and all failure bodies are replaced with fixed content-free values
  before storage and cancellation reconciliation.

## Reconciliation

- Exact Native termination reports bind the original snapshot/session/task/generations and frozen
  digest/classification. They require the original Node and active owner, but no preparation lease.
  They cancel only nonterminal original create tasks, preserve accepted startup receipts, and revoke
  device/browser bindings atomically with the session transition. The committed stopped receipt is
  distinct from persisted/published content; finalization cannot later change its input.

- Reconciliation snapshots are node-owned status summaries.
- Store section names and summary keys in audit logs, not full sensitive state.
- Runtime session summaries contain only session IDs, backend names, neutral resource IDs, and active flags.
- Reconciliation marks inactive native sessions `interrupted` and may enqueue idempotent runtime cleanup without changing that user-visible status; it must not request command replay.
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

## Skill Snapshot Content

- Initial takeover transfer requires the exact authenticated Node, takeover and task identities.
  A live lease is required until committed; terminal replay returns the retained original receipt
  only while its owner remains active and task payload still matches. Status can precede writer
  drain, but capture/publication cannot. All file requests bind the current upload attempt.
  No route accepts caller-selected paths or creates a takeover reservation.

- Node skill preparation downloads require the exact snapshot and its `create_tool_session`
  task, owned by the authenticated Node, with a live leased/running task deadline. A digest,
  account affinity or another task on the same Node is insufficient.
- Downloads derive the user, account and complete materialized tree from the retained snapshot.
  Active owner and starting session are checked under the user storage lock. Terminal/cancelled
  snapshots, expired leases and mismatched task payloads reject each new request.
- Node content routes cannot accept a user-selected tree digest or expose package/state content
  outside that exact preparation tree. Verified files stream from bounded private disk staging;
  manifest/file contents never enter task results, heartbeat or audit details.

- Finalization writes use the assigned snapshot identity after the session is terminal; they do not
  reuse the preparation lease. They require the original session reference and retain the Helper's
  immutable clean/unclean classification. A renewable upload attempt can change its lease only, not
  its full-tree digest. Only the currently bound attempt accepts file writes/completion. Completion
  proves full persistence, never publication or permission to discard pending local state.
  Finalization receipts normalize upload expiry to explicit UTC, including SQLite-backed retries;
  clients must not infer upload authority from their local wall clock.

The original Node may explicitly publish a fully persisted skill finalization through its separate
`/publish` route. This rechecks the same Node/snapshot/active-owner/terminal-session binding as
finalization reads, independent of the expired preparation/upload lease. No body selects a user,
branch, epoch or target head. The committed response distinguishes published, conflicted and detached;
only published changes account heads. Node background transfer/acknowledgement remains a separate
integration gate and this route does not advertise runtime capability.

Managed session creation requires a fresh per-backend `skill_manager` report with strict protocol and
manifest version 1 plus writable-copy, finalization and recovery support. Its create task references
only the snapshot and exact preparation-task UUID under `skill_manager`; content remains on the
snapshot-authorized streaming routes. Missing support or incomplete account takeover denies admission.

Configuration import tasks obtain current directory ownership through their exact leased/running
`config-import-authorization` route before writes. The start route also rejects old tasks that now
include manager-owned skills. Neither payload metadata nor account affinity grants authorization.

New account bindings and backend migrations check directory mode under the same user content lock
before planning legacy writer tasks. Nonlegacy mode returns `MIGRATION_PENDING` before account,
profile or task mutation, even with managed admission disabled. Queued tasks remain subject to
the Node Helper's durable fence; task completion/cancellation is not proof of writer exit.

Task completion/failure and runtime reconciliation participate in skill history clock transactions
before mutating sessions or tasks. Existing storage users are locked in sorted owner order for
multi-user reconciliation; absent usage rows are not created. Stop requests that move interrupted
sessions back to stopping also reacquire snapshot protection. Clock contexts exit before the outer
commit and preserve existing task/revocation behavior. This does not wire snapshot materialization,
writer-quiescence proof, dispatch or capability advertisement.

Managed snapshot preparation renewal uses `/node/skill-snapshots/{id}/lease` with the exact
preparation task UUID and poll `lease_attempt`. It requires the unchanged managed pointer, active
owner, reserved snapshot, starting session and unexpired leased/running task. The existing task row
is locked after the user lock; renewal changes only its deadline, grants at most 300 seconds and
never increments the attempt or revives terminal/expired state. It is preparation authority, not
runtime readiness or permission to replay an ambiguous launch.

Snapshot file staging must release its preliminary authorization transaction before copying bytes,
then reauthorize before releasing the completed private spool to the HTTP response. The immutable
entry comes from the exact retained snapshot tree. Concurrent revocation or content deletion fails
closed; disk copying cannot hold the task/user locks and prevent preparation renewal.

Managed stop progress uses the original snapshot UUID and owner-scoped metadata reads,
independent of task result replay. See `docs/skill-stop-status.md` for the API and evidence boundary.

Native takeover dispatch uses the exact polled task record and attempt. The worker renews the
lease through Helper capture and upload, and never caches transient errors as task failure.
Generic completion accepts only the exact six-field committed takeover result after the initial
checkpoint transaction has committed; generic failure cannot consume a takeover reservation.
Exact replay has no directory lifecycle side effects. The original Helper capture and Server
reservation provide durable recovery even when the worker restarts before reporting completion.

Native session admission now reserves initial account takeover under the existing user content
lock when enabled library content first requires a legacy account. The original affinity Node and
pinned Native backend must match the selected healthy compatible Node; takeover never selects an
empty source on a replacement Node. The session transaction commits only the takeover reservation
and its task before returning MIGRATION_PENDING with an operation ID and explicit no-session
evidence. Repeated attempts reuse the original receipt; no legacy writer is stopped. Existing
migrating state without consistent original reservation/task evidence fails closed. Owner/device
GET /sessions/skill-takeovers/{operation_id} reads bounded metadata without renewing or creating work.
Only committed authority permits a fresh session-creation attempt. Docker takeover admission remains
unsupported until its independently verified writer adapter is integrated.

Deployment tasks use `prepare_account_skills` only with a saved exact attempt/input binding and the
additional Native `deployment_protocol_version=1` capability. Ordinary polling invokes the exact
reservation service for pending original targets; execution is not advertised by the Node yet.
Generic completion/failure must reject both the task type and
any saved binding independently of mutable payload/type. See `docs/skill-deployment-dispatch.md`.

Every deployment manifest/file request includes both exact task UUID and positive current
`lease_attempt`; lease POST uses that same attempt in its strict body. `prepared_input` is not
execution completion. Renewal cannot revive expired, superseded, rebound or disabled authority.

Only dedicated deployment result confirmation can accept the original Helper receipt and current
poll as readiness. Inspection repeats original Node/task/attempt authorization but grants no lease.
Generic results remain forbidden. See `docs/skill-deployment-results.md` for terminal replay and
atomic task/attempt publication; cancellation and supersession must not be inferred from timeout.

Deployment termination intent is a permanent execution fence checked on content, renewal and first
success. Its original authenticated Node may recover the exact directive after lease expiry without
new preparation authority. Only exact permanent Helper drain confirmation can make the task failed
or cancelled. See `docs/skill-deployment-termination.md`; scheduling cannot substitute for drain.

A deployment task's plan digest identifies the saved resolved execution plan when initial takeover
added original manual sources. It must never be compared directly with the unchanged acceptance
plan digest without validating the exact saved discovery receipt and normalized sources first.

## Explicit passive migration recovery contract

Explicit passive backend migration recovery uses a separate administrator-selected request UUID
and exact original logical task ID. Server pins the original task-record UUID itself, preserves
that task/result, and creates recover_tool_account_runtime:<account UUID>:<request UUID>. The
payload is a version-1 binding of recovery task/record, original task/record, Node/user/account,
tool and source/target. Only a terminal original migration still owning the recovery-required
profile can admit a new recovery. Same-key replay is read-only; another key waits for the previous
recovery to end. User content locks serialize acceptance, fresh Node authorization and results.
The current active owner, affinity, legacy directory, no active sessions, source and profile must
still match. A live recovery task lease and exact poll attempt are required before Helper access
and first result acceptance. Success echoes the exact authorization and recovered=true; failure is
fixed content-free metadata. Original failure remains immutable; recovery success advances only
that original profile and preserves account disable. Failed recovery keeps admission closed.
The Helper operation recover_account_migration bypasses generic caches and uses existing
version-2 receipts. Same-boot recovery may settle completed metadata but cannot begin/copy/chown/ACL/
stop anything. Previous-boot recovery requires an already durable whole-migration succeeded receipt,
complete original copy/target phase and ownership evidence, no rollback, and repeated absence of
all recorded current units and cgroups. It rechecks the original account, backup, unchanged receipts
and current boot without rewriting old-boot metadata. A started old-boot migration cannot be promoted
from phase receipts. Missing/old-version evidence, failed/incomplete rollback, foreign/live services,
invalid account, managed fence or absent backup remain recovery-required. Completed target evidence
is rechecked even if the local whole result already says succeeded. This does not authorize
interrupted ownership repair, rollback attestation or backend advertisement.
Finalization file authorization still checks the exact original Node/snapshot/stopped session and
current upload attempt before admission. Its lease metadata inspection avoids whole-user staging
collection while that same user transaction lock is held through verified input reception and byte
publication. Expiry is checked again before writing. This optimization cannot replace current attempt
identity, renew a lease, publish an incomplete tree or advertise runtime support.

Single-file snapshot/deployment downloads use the original tree's normalized object references and
the complete cross-category deletion barrier in `docs/skill-tree-downloads.md`. They must not query
an arbitrary owned digest or reduce availability checks to the requested file alone. Original
Node/task/snapshot/attempt/lease checks before copying and reauthorization afterward are unchanged.


Passive recovery lease renewal uses a separate Node-authenticated POST at
`/node-api/tasks/{task_id}/runtime-migration-recovery-lease`. The exact original authorization
(binding and poll attempt) is mandatory. Under the existing user/task locks, renewal repeats all
current recovery eligibility checks and refuses expired, terminal, changed or foreign authority.
Only the existing task lease deadline changes, by at most 300 seconds; no task, receipt, phase or
account content is created. Reply timings describe a duration from Server time. Node subtracts the
whole request round trip, renews before expiry and cancels Helper work on any uncertain renewal.
This permits bounded long passive inventory checks without adding permission-repair authority or
changing the version-1 binding/result. A committed terminal result wins a concurrent renewal refusal.


## Explicit source restoration verification

`recover-runtime --verify-source` submits `action: "verify_source"`. Its immutable binding is
version 2 with exactly twelve fields, including that action. Default recovery retains version 1
and its original eleven fields; an absent action is never serialized as null. Request keys cannot
change actions. Authorization, renewal and successful results echo the exact versioned binding.
The Helper only verifies an already terminal whole-version-2 failed original with its pre-copy
baseline, immutable failure attestation, copied backup, target/source ownership intents and all
three successful source rollback phases. Repeated complete content/permission/parent checks and
writer quiescence are mandatory; previous-boot records additionally require current unit/cgroup
absence. Verification never repairs permissions or settles incomplete receipts.
Only a fresh matching successful verification marks the original Server profile `rolled_back`,
keeps the source backend and releases that profile's admission gate. Original task/result and
account disable remain unchanged. Failure retains the gate; terminal replay cannot change a newer
profile. This does not implement interrupted repair or enable any runtime capability.


## Explicit interrupted source repair

`recover-runtime --repair-source` submits `action: "repair_source"` and a version-3 binding with
exactly twelve fields. It is mutually exclusive with `--verify-source`; v1/v2 stay unchanged.
The current lease, request key, authorization, renewal and result pin the action. Server success
keeps the source backend, preserves original failure and account disable, and marks only the
original profile `rolled_back`. All existing ownership, inactivity and admission gates apply.
Helper requires a whole-v2 started original, durably completed copy, immutable baseline, exact
backup/content and repeated passive writer quiescence. Independent private repair intent, attempt
and completion records preserve and permanently fence original receipts. Synchronous permission
restoration runs under cancellable lifecycle exclusion; shared parents accept only original
traversal effects and attributable interrupted restoration. Completed retries only revalidate.
Previous-boot repair additionally requires absent recorded units/cgroups. No content, backup,
original outcome or runtime identity is rewritten, and no process writer is launched.
