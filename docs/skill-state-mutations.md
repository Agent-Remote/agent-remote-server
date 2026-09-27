# State reset and restore transaction contract

The approved root design authorizes explicit item and account-directory reset/restore. Current-state
selection must remain read-only and show exactly the account's effective revision (including pins),
source identity, installation epoch, optional branch/head, branch epoch/expiry and directory mode/head/
epoch. Item selection may inspect/reset a disabled source without enabling it. Directory mutations
select only effectively included library sources and active enabled account-local sources.

Mutation requests carry a user-scoped idempotency key, explicit account/item or directory selector,
expected library generation and the complete selected directory/branch preconditions. Restore also
selects a retained checkpoint. The user storage lock covers selection, checks, quota/content validation,
new checkpoints, epoch/head CAS and immutable operation receipt. A dry-run returns all metadata path
changes and affected identities but creates no branches, uploads, checkpoints, epochs or receipts.
The CLI must confirm that concrete preview once unless the user already supplied noninteractive consent.

Item reset uses the selected immutable original package/local initial view. Item restore requires the
same user/account/stable source/name/revision; old installation epochs are allowed only for the same
stable installation, whose source identity cannot change. Directory reset clears auxiliary roots and
resets the effective items, preserving nonselected member subtrees and branch heads. Directory restore
requires exact selected source/name/revision membership, restores auxiliary data, and preserves current
nonselected members without enabling them. Any dependency failure rejects the entire command.

Both operations publish a new directory checkpoint and new selected branch heads. Selected branch
epochs advance even if bytes are unchanged; expired/uninitialized selected branches can be reset or
explicitly restored. Directory-scope commands also advance the directory epoch. Item commands change
its directory head without invalidating unrelated branches' epochs. Existing checkpoints remain
recoverable. The library generation and enabled/pin rules do not change. Source collisions never grant
identity takeover by byte equality.

Migration 0033 adds `skill_state_operations`: immutable user-keyed reset/restore receipts with request
digests, action/scope, owner/account-bound optional source checkpoint and required result directory
checkpoint, and the typed response as JSONB. Composite foreign keys enforce source scope and exact
owner/account on both references. Downgrade refuses while receipts exist. No existing tables are
silently cascaded or discarded.

Changing a directory head marks its retained unresolved publication plans superseded with a reset/
restore reason. It does not execute their input or choices inside the state command. A later explicit
resolve of that invalidated attempt can recompute a replacement from the original input, without
applying/copying choices. Old Node publication retries remain inspection only. Epoch-mismatched late
session inputs detach under the normal publication rules.

This contract does not authorize public managed admission, migration automation, retention or Node
capability advertisement. Those remain separate cross-repository work and acceptance requirements.

## Implemented HTTP contract

These active-user-token-only routes are available under `/api/v1/skills/state`:

- `GET /current?account_id=…&skill=…` returns `selector` and the complete `precondition`. Use
  `scope=account-directory` instead of a skill for the enabled effective set. Read-only selection
  does not initialize missing branches and preserves account/tool pin provenance.
- `GET /diff` takes the same selector plus bounded `limit`/path `cursor`, comparing the currently
  selected branch head (or directory head) to its original baseline. Missing/expired selected state
  fails explicitly; it never reads a different revision's head as a fallback.
- `POST /commands` accepts `action=reset|restore`, `selector`, `expected` copied from the current
  view, `idempotency_key`, strict `dry_run` and, for restore only, `checkpoint_id`.
- `GET /operations?key=…` recovers the immutable original accepted response.
- `GET /operations/{operation_id}` reads that same immutable receipt by UUID for generic CLI status
  and other logged-in devices. Both queries filter by authenticated owner, never execute the command,
  and preserve the original result after later state changes.

A result contains `operation_id`, `status=preview|published`, `action`, `before`, result tree/checkpoint
identities, `changes`, `branch_changes`, `affected`, `directory_epoch_advances` and the number of
actually `superseded_conflicts` (zero for a nonmutating preview). The standard envelope marks only
actual committed commands as committed. A new key with stale expectations returns
`STATE_PRECONDITION_CHANGED` plus the new current preconditions. Replaying an accepted key returns
its original result before evaluating today's rules or heads; changing the request under that key
returns `IDEMPOTENCY_CONFLICT`.

`changes` shows all metadata changes against the stored account directory. `branch_changes` also
compares each selected branch against its own old head: when the directory shows r3 but the user
pins r2, clearing r2 learning data must still appear even if that data was absent from the directory.
An expired branch baseline is explicitly unavailable rather than replaced by an invented empty tree.
Both representations retain paths/digests/sizes, never file bodies. Previews validate complete
link dependencies, per-scope limits, actual result bytes and aggregate state storage admission without
creating reservations or expiring uploads.

Implementation is split into selection, plan construction, atomic application and command orchestration.
Original package/local initial revisions remain immutable. Recovered known invalid/deleted skills keep
format diagnostics instead of being rewritten. Source-set mismatch reports both expected and actual
name/ID/revision tuples. Actual branch/head CAS rechecks the exact preview values after planning.

Evidence: service/API tests cover preview without writes, concurrent duplicate replay, stale requests,
late-CAS rollback, same-source reinstall recovery, wrong-account/scope/revision rejection, expired
state recovery, directory auxiliary reset/restore, disabled-member preservation, link dependency
rejection, pin-selected old-branch loss previews, real user-token authorization and late-session
input detachment. Database tests reject substituted owner/source-scope/result-scope references.
The 48 combined state-command/resolution/publication cases pass on SQLite and PostgreSQL 17.
Migration 0033 upgrade/down/up and ORM schema comparison passed; guarded downgrade preserved all
15 tested receipts and the schema version. Full Server gate: 628 passed, 15 skipped, 76.41% coverage,
with format/lint/type/docstring/whitespace checks passing.

State migration, retention/GC, account takeover, Node/CLI integration and real backend acceptance
remain unfinished. Reset can explicitly initialize a selected target revision without migration,
but that user action is not an implementation of automatic or incremental state migration.


Version preparation now has its own retained input records. Reset/restore also marks pending
preparation as superseded under the same transaction, without replaying that migration. The reported
`superseded_conflicts` counts both finalization and version-preparation conflicts. The original
preparation receipt remains immutable; lookup reports its current status separately. See
`docs/skill-state-migrations.md` for first-use preparation and remaining incremental/resolve work.
