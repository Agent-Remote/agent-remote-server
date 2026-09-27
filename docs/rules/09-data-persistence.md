# 09 Data And Persistence

Node-local reclamation authorization adds no database table or retention root. It is a fresh bounded
content observation, not a permanent content lease or backup certificate. The storage user lock
covers reference checks and complete physical verification, including cancellation joining the I/O
thread. Existing history, object deletion barriers, upload attempts, quotas and clocks are unchanged.
Expired, retired, missing or corrupt content never grants local reclamation merely because its old
finalization receipt still exists. A Node must durably bind a timely authorization to its exact local
reclamation intent before releasing redundant bytes; this API alone cannot perform that operation.

Migration 0049 records owner/operation/account-bound deployment attempt chains and idempotent retry
receipts, as specified in `docs/skill-deployment-attempts.md`. Existing operations remain unmarked;
new acceptance and initial attempts share one savepoint. Successor creation, current projection and
retention clocks commit together. Never rebuild original inputs, rewrite terminal predecessors or
permit retryable permission/unsupported/conflict outcomes. Downgrade refuses recorded history.

Migration 0048 retains one immutable termination observation per managed snapshot, binding incoming
digest and clean/unclean classification before upload. Original owners/generations/task stay on the
immutable snapshot; every replay revalidates them. No content, host paths, cascade deletion or guessed
historical receipt is introduced. Downgrade refuses recorded observations. See
`docs/skill-session-termination.md` for transaction and retention boundaries.

Managed Native start acknowledgements reuse `node_task_results`; no additional schema is introduced.
Ready outcomes use a fixed ten-field result and stopped outcomes a fixed nine-field error. Both bind
the original task record, snapshot, session/account/backend, canonical unit and poll attempt. The
existing user history transaction and locked session/task serialize first publication. First
acceptance moves the snapshot to `started` for readiness or `finalizing` for stopped startup.
The snapshot phase and task/session outcome commit together. Identical
terminal replay returns the saved outcome without changing session state or repeating revocation;
different outcomes/attempts conflict. A persisted snapshot reference also identifies managed tasks
when their payload marker is missing, preventing fallback to generic unguarded completion.

## Current Persistence Boundary

Migration `0039_skill_account_takeover` follows `docs/skill-account-takeover.md`: one owner/account
takeover receipt binds exact Node task, directory epoch, immutable writer inventory, Helper capture
identity, scoped upload and the committed initial directory checkpoint. Composite keys bind the task to its Node and prevent cross-owner upload/checkpoint substitution.
The service separately validates exact task payload ownership under the user lock; NodeTask has no
relational user column. Receipt presence blocks downgrade before mutation.

Business tables require an explicit schema design and migration plan. ORM models and Alembic migrations must be updated together.

## Database

Skill manager persistence is authorized by the reviewed user skill design. Reversible migrations add
private content/upload/quota records first, then installation/revision/override, account
state/directory checkpoint, operation/deployment, snapshot/finalization and conflict records.
Migration `0026_skill_content_storage` adds user-private object/tree references, idempotent upload
leases and independent package/state byte reservations. Content completion and quota accounting
commit together after every file passes streaming verification. A user-row write lock serializes
reservations and future GC reference changes. Knowing a digest never authorizes a read.
Foreign keys retain content metadata until explicit user-data deletion handles the storage volume. Content blobs live in independently protected
persistent storage. Ownership, revision affiliation and concurrency preconditions must be enforced
before references are published. Session deletion must not cascade-delete pending finalization;
current references, pins and unresolved conflicts protect content from garbage collection.

- Migration `0027_skill_library` adds stable installation identities, archived installation
  epochs, immutable revision provenance, separate source observations and activation history,
  tool/account field overrides and idempotent operations. Composite foreign keys bind the
  owner, skill, revision and account tool. A partial unique index reserves each active name;
  a different archived source never shares the replacement's ID or runtime state. All mutations
  acquire the storage user lock before reading generation or establishing content references.
  Library generation and eventual account state epochs remain independent. Qualified deletion
  of a disabled, unbound account without session history removes only its override rows under
  the same user lock and advances library generation; other account rules and content remain.
  Runtime state references must additionally block account deletion when that layer is connected.
- Migration `0025_cli_login_sessions` adds `cli_login_sessions`: UUID, user ID, unique
  current access-token ID, unique keyed refresh-token hash, absolute expiry and timestamps.
  Both foreign keys cascade on deletion; access-token history retains a nullable session
  reference so logout racing with rotation revokes the same session. No plaintext credential
  is stored. Downgrade
  removes remembered sessions; existing access tokens retain their short expiry.
- PostgreSQL is the control-plane database.
- SQLAlchemy async engine is required.
- Alembic owns schema migrations.
- Migrations must be reversible unless explicitly documented.
- UUID primary keys are generated by application code unless a migration explicitly documents a database default.
- JSON-like structured fields should use PostgreSQL JSONB in migrations.
- Sensitive binary fields must be encrypted before they are stored.

Runtime selection persistence:

- `nodes` stores the administrator allowlist, default backend, runtime policy, and latest capability snapshot.
- `tool_accounts.runtime_backend` is nullable until first binding and is then treated as pinned state.
- `sessions` stores the effective backend, neutral runtime resource ID, and optional replacement relationship.
- `device_sessions` stores the exact user/device/tool-session/node binding, macOS platform,
  lifecycle state, generation, bounded lease, expiry, machine-lock time, stop metadata,
  `authorization_mode`, `authorization_policy_version`, and `authorized_at`.
  Generation uses `BIGINT`; database checks enforce the shared signed 64-bit protocol limit and
  reserve its maximum value for terminal state only.
- Partial unique indexes allow only one non-terminal `device_sessions` row per
  `tool_session_id` and one per `device_id`. Terminal records remain available for audit and
  retention and do not occupy either live binding slot.
- `device_sessions.tool_session_reference_id` preserves the original Claude session UUID for
  terminal history. The relational `tool_session_id` uses `ON DELETE SET NULL`, so deleting an
  inactive tool session cannot cascade-delete device binding history before retention cleanup.
- `device_session_approvals` stores only legacy `per_application_approval` data. New
  `session_full_trust` bindings never insert wildcard, sentinel, empty-digest, or Device-identity rows.
- `sessions.device_control_protocol_version` records only whether the managed MCP configuration
  was included at session creation; it contains no connection or authorization material.
- Terminal `device_sessions` and their approval summaries are eligible for bounded retention
  cleanup only after their configured stop-time cutoff. Device-session audit rows use an independent
  retention period that cannot be shorter than session retention. Both periods default to disabled;
  production device control requires operators to choose explicit non-zero values.
- Device and Node deletion must not cascade-delete retained DeviceSession history. They remain
  undeletable while any retained binding references them; tool-session deletion instead nulls the
  relational foreign key while preserving `tool_session_reference_id`.
- Migration task metadata and backup paths are non-secret operational metadata; account login state remains node-local.
- `ego_browser_devices` stores only the independent device public keys, release and credential
  profiles, version/capability metadata, monotonic helper-allowlist revision and roots digest,
  verified learning-bundle digest, online state, and revocation metadata.
- `ego_browser_bindings` stores the exact user/device/tool-session/node identity, full-trust policy,
  platform and capability snapshot, generation, lease health, absolute TTL, and terminal metadata.
  Partial unique indexes enforce one live binding per device and tool session while retaining
  terminal history. It never stores scripts, output, artifacts, browser content, URLs, or paths.
- Explicit binding deletion removes only terminal binding metadata, its content-free request ledger,
  and delivered revocation outbox rows. Device deletion removes a revoked device and its retained
  credentials only after no binding history remains. Both operations retain their audit
  records and must not bypass lifecycle or revocation-delivery checks.
- Browser relay tickets, request sealing keys, encrypted frames, permits, proof challenges, and
  connection state remain short-lived in Redis or process memory. The persistent request ledger
  stores only outer request identity and terminal delivery state for replay prevention.

## Repository Layer

- Repositories own direct SQLAlchemy access.
- Services orchestrate repository calls.
- API handlers must not build ad-hoc SQLAlchemy queries for business objects.

## Redis

Redis is required by the broader project and carries short-lived distributed coordination.

Redis is used for:

- Task lease coordination.
- Distributed locks.
- Polling throttles.
- Short-lived task state.
- Device relay key exchange, one-time tickets, and cross-worker binding revocation notifications.
- Ego-browser role-bound relay tickets, cross-worker endpoint coordination, bounded old-generation
  revocation markers, and notifications in an independent key namespace.

## Secrets

Secrets are supplied through environment variables or deployment secret stores.

Do not persist:

- Raw tokens.
- Private keys.
- Tool account login state.
- Browser cookies or profiles.
- Device-control screenshots, zoom images, input, clipboard data, window titles, coordinates,
  image hashes, plaintext relay payloads, or one-time connection secrets.
- Device-control ephemeral certificates, SPKI pins, relay tickets, exporter context secrets, or
  encrypted relay frames. These remain short-lived in Redis or process memory only.
- Ego-browser heredoc text, stdout/stderr, screenshots, browser state, local paths, per-request
  session keys, key wraps, relay tickets, permits, or encrypted relay frames.

The runtime skill schema is specified in `docs/skill-runtime-state.md`. Account directory and branch
heads are independent from library generation. Complete directory trees, item views, immutable
membership, exact session snapshots and finalization receipts carry composite ownership constraints.
Snapshot/session references never cascade-delete pending or retained skill state. Services must use
the existing storage user write lock before reserving snapshots, advancing heads or releasing roots.

Migration `0029_skill_finalization_uploads` adds owner/digest/scope-constrained transfer leases.
Expired attempts may be replaced under the same user lock, while the unique finalization remains
immutable. Complete input checkpoints and format diagnostics commit with durable object references;
no account head advances on ingestion. Transfer links must be explicitly released by retention,
never cascade-deleted with a session or upload lease.

Migration `0030_account_local_skills` introduces account-only candidate identities and immutable
initial state-tree views. Runtime branches use an exclusive source check to choose either library
installation/revision or local skill/revision foreign keys. Active local names are account-unique;
staged concurrent candidates retain separate identities. Activation is a publication operation.
Downgrade is guarded when local identities exist because no lossless user-library representation
exists; the guard runs before schema mutation.

Migration `0031_skill_publications` retains immutable publication attempts and exact branch
preconditions, including comparison content roots for unresolved conflicts. All heads and local
candidate activations commit together under the storage user lock and head/epoch CAS. An unclean
input or invalid changed-branch epoch detaches the entire input. Downgrade refuses while attempts
exist rather than deleting conflict/recovery references. An unchanged branch does not count as a
write merely because its epoch or current revision changed after session reservation.

Migration `0032_skill_resolution_plans` stores account-bound plans, individually rooted custom-content
choices and immutable user idempotency receipts. Plan revision CAS, authorization and all final head
checks run under the storage user lock. No dry-run writes plans or receipts. Conflicted publications
may become published only through complete explicit plans; stale targets create new attempts and
retain the superseded plan/input. Downgrade refuses while resolution history exists before mutation.

Migration `0033_skill_state_operations` records immutable reset/restore receipts under a user key.
Composite owner/account/scope foreign keys protect the optional restore source and required result
directory checkpoint. Receipt/history presence blocks downgrade. The transaction increments selected
state epochs (and directory epoch for directory scope) while preserving original checkpoints and
library rules. Exact CAS and receipt insertion share the storage user lock and rollback boundary.

Migration `0034_skill_branch_preparation` records actual last-effective snapshot members and
independent first-use branch preparation receipts. Same-owner/account/installation/epoch foreign
keys protect source and target branches; complete state trees retain all three comparison sides.
Only successful snapshot reservation changes the effective ledger. Failed migration does not move
heads. Reset/restore supersedes pending preparation while retaining its inputs. Downgrade refuses
when either table contains data. See `docs/skill-state-migrations.md` for the schema contract.

Migration `0035_skill_incremental_migration` extends preparation records with optional source-branch
baseline and target-branch current checkpoint foreign keys and a successful migration sequence.
The unique sequence scope includes source/target IDs, both branch epochs and directory epoch.
Existing successful forward preparations receive sequence 1 without changing immutable responses.
Only complete ready forward/incremental records may have a sequence; unresolved attempts never do.
Downgrade refuses incremental history before mutation. See `docs/skill-state-migrations.md`.

Migration `0036_skill_migration_resolution` adds owner/account/migration-constrained upload bindings, custom-content
roots, monotonic plans, selector choices and immutable operation receipts. Choices may reference
only custom content granted to that exact migration. Plan replacement uses revision CAS and a
savepoint. Downgrade refuses before mutation if any of these five tables contains retained history.

Migration `0037_skill_checkpoint_provenance` adds nullable historical state/directory epochs and
exact item backing-directory references, per `docs/skill-checkpoint-provenance.md`. Legacy unknown
provenance stays null. A composite owner/account/scope/content-digest FK binds backing directories;
retiring bytes does not erase this identity proof. Every new checkpoint writer records the actual
creation epoch and explicit backing context where one exists. Downgrade refuses while any evidence
would be lost. Metadata provenance references do not themselves define permanent content GC roots.

Migration `0038_skill_migration_replacement` adds nullable owner/account-bound predecessor and
replacement references plus supersession reason to branch preparations, per
`docs/skill-migration-recomputation.md`. One original attempt has at most one direct recomputation;
self references and replacement metadata on active rows are rejected. Legacy unknown relations remain
null. Downgrade refuses before mutation whenever any replacement evidence would be lost.

Library lifecycle invalidation and successful-migration replacement reuse 0038 metadata without a
schema change. Only status/reason/replacement change; retained comparison inputs, original JSON,
choices and custom grants stay attached to the original record. New success supersedes exact
owner/account/installation/direction/epoch matches, excluding its recomputation parent until that
parent's replacement CAS. Configuration, publication, invalidation and receipt share one savepoint.

Migration `0040_skill_retention_clocks` adds nullable UTC last-release timestamps to retained skill
history identities as specified by `docs/skill-retention.md`. It does not backfill legacy times.
Downgrade checks all affected tables before removing any column and refuses to discard real clocks.

Migration `0041_skill_history_retirement` replaces historical comparison tree foreign keys with
stored generated retained-digest references controlled by nullable retirement timestamps. Original
digests and authorization primary keys remain immutable. Active status checks prohibit retired live
snapshots, pending finalizations and unresolved comparisons. The finalization/checkpoint foreign key
continues to bind exact owner/account/scope/identity/content evidence using content_digest. Downgrade
preflights all six tables before touching constraints and refuses any existing retirement history.

Internal checkpoint retirement reuses the existing 0028 retained/tree_digest and branch expired
schema; no migration is needed. The 0041 finalization FK already targets immutable content_digest.
Parent, backing, membership and receipt identities remain intact after content retirement. Services
must validate retained-history dependencies before atomically clearing tree_digest and setting
retained=false; retiring a branch head also sets expired=true in the same savepoint.

Internal directory compaction reuses checkpoint provenance and existing complete-tree/member models;
no schema change is required. It creates new directory and item identities instead of editing old
digests/members, preserves branch and directory epochs, and uses existing head CAS under the user
retention savepoint. New equivalent checkpoints record the actual creation epoch and backing identity.
Old inputs, receipts and incremental baseline references remain immutable and retain their content
until independently eligible retirement. See `docs/skill-directory-compaction.md`.

Migration `0042_skill_tree_retention` adds nullable last-release clocks to complete stored trees.
Legacy timestamps stay NULL; downgrade refuses populated clocks before schema mutation. Fresh upload
completion and the existing retention mutation boundary maintain tree clocks under the user lock,
while committed replay remains read-only for clocks. Tree expiry never overrides retained historical
foreign keys or upload/object protection. See `docs/skill-tree-retention.md`.

Migration `0043_skill_content_deletions` adds durable user/digest deletion tasks, original task UUIDs,
category masks, progress and bounded retry metadata per `docs/skill-content-gc.md`. Pending tasks are
unique by user/digest. Available objects remain quota-bearing; marked deleting objects have already
released category quota and survive solely as admission barriers until durable completion. Upgrade
refuses unknown preexisting deleting markers; downgrade refuses any task or marker before mutation.

Migration `0044_skill_prune_claims` adds owner/account/scope-bound tree/object cleanup claims per
`docs/skill-prune-candidates.md`. Claims attach to actual state content rows through explicit
ON DELETE CASCADE foreign keys so deletion destroys only the derived cleanup authority; a later
same-digest upload cannot inherit it. Claims do not protect content or replace durable operation
receipts. Upgrade never backfills historical digest guesses; downgrade refuses outstanding claims.

Migration `0045_skill_prune_operations` adds immutable prune acceptance, complete disclosure rows
and same-owner deletion-task links per `docs/skill-prune-api.md`. It adds only empty operation tables
and the deletion task's (user_id,id) uniqueness; existing tasks are unchanged. Receipt presence
blocks downgrade before mutation. Operation metadata is not a retained-content root and no original
confirmation credential is stored. All rows share the original cleanup transaction.

Storage diagnostic reads use existing usage, history and deletion-task tables under the same owner
lock as reference/GC mutations. They must not create missing usage rows, increment lock_version or
write guessed release timestamps. Completed deletion bytes are cumulative task evidence, never
available disk space. This read-only additive API requires no migration.

Migration 0047 adds immutable account configuration plans and normalized owner-scoped revision
references for library operations. See `../skill-deployment-plans.md`. Plan rows are acceptance
history, not live account bindings; only pending/retryable operations protect their content.
Unknown or inconsistent plans must fail retention analysis rather than recompute current rules.

Migration 0046 adds nullable skill_finalizations.persisted_at, written once when upload completion
atomically verifies content and creates its incoming checkpoint. Old rows remain unknown; no backfill
from created_at/updated_at is allowed. A timestamp cannot coexist with upload_pending. Publication,
retirement and replay preserve it. Downgrade refuses any recorded timestamp before changing schema.

Snapshot preparation renewal reuses NodeTask.lease_until and retry_count without a schema change.
The user storage lock and exact task row lock cover authorization and deadline update; the original
snapshot, input references and attempt stay immutable. File download staging commits its preliminary
read authorization before disk I/O and rechecks the original snapshot before response. Concurrent
revocation never creates a new content reference or restores a retired snapshot.

First-use Native session admission reuses migration 0039 takeover records without a schema change.
The existing session savepoint commits only original takeover reservation and its task before the
MIGRATION_PENDING error is returned. Explicit reservation_committed/session_created metadata is
emitted only after commit. Errors and final capability withdrawal roll back that reservation.
Owner progress reads join original takeover/task in one statement without changing lease or content
references. See `docs/skill-session-admission.md` for retry and preserved-source boundaries.

Automatic deployment supersession reuses `skill_operations.replacement_id`, with no new schema.
The first replacing accepted operation must belong to the same owner, have a greater generation and
contain saved changed selection for an original target. Status and retention validate this relation;
monotonic generations prohibit cycles. Attempts and exact original plan rows are not rewritten.
Parent supersession keeps active target roots until independently terminal observations arrive.

Migration 0050 adds exact deployment task bindings as specified in
`docs/skill-deployment-dispatch.md`. A complete input is an independent directory checkpoint with
normalized members, never an invented session snapshot or account head. Active attempts protect it;
terminal bindings retain metadata only. Retry reuses the original input, and downgrade refuses
recorded bindings. User locks/savepoints cover preparation, task, binding and reference clocks.

Deployment preparation success reuses `node_task_results` without a migration, as specified in
`docs/skill-deployment-results.md`. User/task locks serialize its first immutable receipt. Task and
attempt readiness, projection and retention clocks share one savepoint. Duplicate or inconsistent
terminal receipts fail closed; historical inspection never reacquires content references.

Deployment task `expired` is a known non-drained retention state. It continues protecting the
original directory and members even if the attempt projection failed or the operation was
superseded. It does not authorize retry overlap, preparation, lease renewal or success confirmation.
Unknown task states still fail retention analysis rather than silently releasing content.

Migration 0051 adds one immutable original-attempt termination intent as specified in
`docs/skill-deployment-termination.md`. It revokes execution without releasing input. The existing
NodeTaskResult stores the later exact drain confirmation plus final observed poll; task/attempt,
projection and retention clocks share one savepoint. Recorded intents block downgrade.

Deployment discovery follows `docs/skill-deployment-discovery.md` and migration 0052. New pre-takeover
acceptance saves the expected initial directory epoch beside the immutable original target. Initial
takeover publication alone may resolve it, with normalized original local-revision references.
Never overwrite the accepted plan or infer discovery eligibility for historical/managed targets.

Ordinary deployment polling uses existing attempt updated_at only to rotate bounded candidate
examinations, as described in `docs/skill-deployment-scheduling.md`. It is not a lease, execution
receipt or retention release clock. Candidate reads grant no authority; each owner transaction
reloads the attempt chain and excludes any existing task binding before changing progress. No new
schema or inferred Helper drain is introduced by scheduling.

Passive backend migration recovery reuses NodeTask/NodeTaskResult and the original account profile.
The original failed task/result remain immutable. A distinct recovery binding pins both logical
and record identities; only the current profile's recovery task can settle its target backend.
Same-key/status/terminal replay lock existing usage rows without inserting or advancing counters.
First admission/result mutation uses the existing user write lock before the recovery task lock.
No content, private paths or credentials enter the recovery binding or result.

Migration 0053 extends the existing exact termination row with nullable incoming_digest and a fixed
capture_error. Exactly one frozen digest or pending-capture reason is required by SQL. No historical
receipt is invented. A first frozen observation may fill the missing digest under the existing owner
lock; classification remains immutable and later failed-capture reports cannot erase frozen evidence.
The snapshot remains finalizing and continues protecting pending local content. See
`docs/skill-session-termination.md` for the independent process/data boundary.

Migration 0054 adds the reproducible upload declaration projection specified in
`docs/skill-upload-object-index.md`. Complete index creation/version publication shares the original
user-locked upload transaction; legacy inputs backfill only after canonical manifest/digest validation.
Composite ownership/upload/tree/scope foreign keys exclude cross-input membership. Single-file reads
must omit the canonical large JSON column. Indexed entries grant only original object admission;
complete still validates the original whole manifest and all bytes. Terminal transitions remove the
projection without removing original audit metadata. Downgrade drops only derived metadata.
Retention reads the projection only through its staged parent and checks the atomically recorded
unique object count. Missing/extra references fail the complete analysis; projection rows themselves
remain non-roots. The existing row budget includes them. Legacy staged manifests retain their JSON
budget and validation; terminal manifests and indexed full JSON are not materialized for graph reads.

Migration 0055 adds only the partial unavailable-object lookup index specified in
`docs/skill-tree-downloads.md`. Existing complete-tree/object foreign keys and atomic completion/GC
transactions remain the file membership authority. The index changes no rows, clocks, quota or
retention roots. Downgrade removes only the index; SQLite test metadata uses the same predicate.


Passive recovery lease renewal uses a separate Node-authenticated POST at
`/node-api/tasks/{task_id}/runtime-migration-recovery-lease`. The exact original authorization
(binding and poll attempt) is mandatory. Under the existing user/task locks, renewal repeats all
current recovery eligibility checks and refuses expired, terminal, changed or foreign authority.
Only the existing task lease deadline changes, by at most 300 seconds; no task, receipt, phase or
account content is created. Reply timings describe a duration from Server time. Node subtracts the
whole request round trip, renews before expiry and cancels Helper work on any uncertain renewal.
This permits bounded long passive inventory checks without adding permission-repair authority or
changing the version-1 binding/result. A committed terminal result wins a concurrent renewal refusal.


Migration 0056 adds the already-used `migrating` account status to the database check constraint.
All existing binding/account states remain valid and no rows or migration profiles are rewritten.
ORM metadata enforces the same state set so SQLite tests cannot silently accept missing production
status support. Downgrade holds the account table lock and refuses any migrating row before changing
the constraint. Runtime migration profiles remain the independent writer-admission authority.
