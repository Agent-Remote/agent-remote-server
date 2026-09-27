# Backend migration admission and retained recovery

Backend account migration changes ownership of the same persistent account directory. A failed task
does not prove that its backup/ACL services have exited or that source permissions were restored.
This matters before initial skill takeover: a delayed legacy task must not become another writer
while migration recovery is unresolved.

The Server retains `runtime_migration` in the existing account profile. New migration acceptance,
binding, session creation and config-import planning/authorization use the same user content lock.
They refresh the account and profile after acquiring it. A missing migration permits ordinary
admission; a recorded migration permits it only after `succeeded`. Pending, recovery_required,
historical failed and malformed migration records return `RUNTIME_MIGRATION_PENDING`. Editing
`account.status` does not remove this gate. Rejection occurs before a replacement task or account
configuration is written. Existing sessions are not stopped by this admission check.
Node config-import authorization still verifies the original task, active owner, current lease and
payload before exposing migration diagnostics. Expired or unleased requests keep their original
not-found response; starting an already terminal task keeps the generic terminal conflict.

Migration completion also takes the same user lock before recording a task result. It requires the
original task, user, account, affinity Node, source backend and target backend. Success must explicitly
confirm the selected target. The first accepted result is immutable: exact replay is read-only even
after a later migration starts; different success/failure content is rejected with
`RUNTIME_MIGRATION_RESULT_CONFLICT`. An invalid success does not publish a result or reopen admission.

An accepted failure preserves the original backend and records `recovery_required`; the account
stays migrating unless already disabled. A later successful result cannot replace that failure.
Success does not undo an intervening account disable. These checks preserve the existing task/result
transaction and require no new schema. Historical failed receipts are not upgraded to rollback proof.

The Helper independently checks retained local migration/copy history before new legacy Native or
Docker binding/session mutations and config-import writes. Started, copy-only, failed-copy and
incomplete migration evidence produce `STATE_MIGRATION_PENDING`, including after Helper replacement.
Runtime admission checks the skill takeover fence first. Completed exact task/import replay remains read-only;
an unrelated account with valid separate history remains usable. Worker reports fixed diagnostics
for either local or Server admission denial without copying raw Helper/HTTP messages.

The explicit passive recovery protocol below builds on these guards. Interrupted rollback,
unknown writers and source permission attestation remain unimplemented. Deleting receipts or changing profile state is not a supported repair procedure.
Original account bytes and backup remain retained; no new backend capability is advertised.

`tests/test_runtime_migration_recovery_gate.py` exercises authenticated HTTP acceptance, failure,
replay, malformed completion, replacement denial and display-status bypass attempts.
`tests/test_runtime_migration_admission_races.py` observes actual PostgreSQL blocking PIDs and proves
that waiting admission refreshes an old ORM profile after commit, while rollback leaves it usable.
Node's `migration_admission_linux_test.go` covers each writer entry point after Helper replacement;
the actual systemd cancellation test additionally proves denial while the ACL service remains live.

Node now persists versioned copy/ACL writer authority before each privileged phase. It binds a unique
launch description to one systemd invocation, preserves live writers after cancellation, and can
observe the original retained invocation through a replacement Helper Engine without restarting it.
Whole-migration completion requires all applicable phase proofs; missing or orphaned evidence keeps
local admission closed. Generic Helper result caching no longer bypasses this original authority.
See Node `docs/skill-backend-writer-recovery.md`. These records are prerequisites for recovery; the explicit recovery path below does not accept them as source rollback proof.

New Node writer-version-2 ownership intents also cover direct Lchown before the first ACL command.
Exact same-task Helper replay can now converge original same-boot work that completed before its
copy/whole-migration result was recorded. It does not launch missing commands or repeat ownership.
A recorded rollback intent, even without an ACL receipt, prevents inferring target success. This
local metadata recovery does not override a terminal Server failure: original result immutability
and the explicit recovery-required gate remain in force. The administrator recovery task below can settle completed target work; source permission
attestation remains required for rollback cases.

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


Administrator routes are `POST /tool-accounts/{account_id}/runtime-migration/recover` with
`original_task_id` and a nonzero UUID `request_id`, and read-only
`GET /tool-accounts/{account_id}/runtime-migration/recover/{request_id}`. They return the typed
original binding and recovery task status. Node polling carries that same binding;
`GET /node-api/tasks/{task_id}/runtime-migration-recovery-authorization` additionally returns the
current lease attempt. Generic start/complete/fail enforce the same guard. Result publication is
atomic with task/result/account/profile mutation; no new schema or content receipt is introduced.

Evidence: `test_runtime_migration_explicit_recovery.py` covers authenticated HTTP admission,
immutable original failure and recovery result, exact historical replay after a newer migration,
failed-check retry by a distinct key, disable preservation, lease expiry, owner/affinity/backend/
profile/task changes and nonlegacy refusal. `test_runtime_migration_recovery_races.py` observes
actual PostgreSQL blockers for same/different-key acceptance and post-lock profile refresh.
Worker socket/HTTP tests and real isolated systemd completion tests exercise the matching protocol
in Node; CLI contract tests verify user authentication, exact request JSON and read-only status.
The separate cross-component acceptance below exercises the real CLI and production daemons.

## Cross-component passive recovery acceptance

The opt-in recovery acceptance must use the real administrator CLI over HTTP, the shipped
unprivileged Node worker unit and root Helper unit in a disposable systemd/cgroup container.
Inject an original Helper reply loss only after actual copy/ownership completion; the ordinary
Worker must record/report its failure. Then restart both daemons and route recovery directly
through the authenticated production Helper socket. No migration receipt/result may be seeded
as proof. Check original task immutability, disable preservation, no additional writer launches,
and unchanged complete account/backup bytes and metadata. A missing-backup case must remain
blocked; lost recovery acceptance/completion responses must be read back under the original key.
This test uses synthetic account content and no model credentials/inference. It proves backend
recovery transport/lifecycle, separately from actual skill learning, SSH and Docker Sandbox.

Run from the Server repository:

```sh
AGENT_REMOTE_RUN_RUNTIME_RECOVERY_TEST=1 uv run pytest -q tests/test_runtime_migration_recovery_live.py
```

Sibling Node and CLI repositories are used by default; `AGENT_REMOTE_TEST_NODE_REPO` and
`AGENT_REMOTE_TEST_CLI_REPO` can select explicit checkouts. Both lost-replies and missing-backup
cases passed on 2026-09-25. The lost-replies case rejects one completion before commit, then loses
the next committed completion response, requiring a fresh lease authorization and immutable replay.
CLI submission response loss emits its dedicated version-1 JSON error with the original identities,
unknown acceptance and exact status command; successful status lookup confirms that same request.


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

The current lease and inventory path passes real CLI/production-daemon acceptance for reply loss
and missing backups (2026-09-26, two cases, 24.93 s). Current binaries/service files were mounted
into the existing disposable Native dependency image; this does not test a fresh dependency install
or actual Claude inference. PostgreSQL tests pass six concurrency cases and a separate real 0056
upgrade/downgrade test; the latter rejects downgrading while any account is migrating and preserves
all rows/constraints. The old `online` Node fixture status was corrected to `healthy`. Logs:
`/tmp/skill-current-recovery-daemon-cached.log`, `/tmp/skill-migration-lease-postgres-final.log`.
