# User conflict resolution

The user query, content and resolution HTTP endpoints are implemented behind
`SKILL_MANAGER_ENABLED`. They require an active user token; device and Node credentials cannot
select resolution sides. This completes the Server finalization-conflict flow, not the remaining
state migration/retention, CLI or Node runtime integration. Explicit reset/restore is now documented
in `docs/skill-state-mutations.md`.

## Inputs and queries

All routes below have the prefix `/api/v1/skills/state`:

| Method and path | Result |
| --- | --- |
| `GET /conflicts?account_id=…&limit=…&cursor=…` | Bounded unresolved/superseded attempts, scoped to one owned account |
| `GET /conflicts/{id}` | Original inputs, exact branches/epochs/heads, plan revision/choices and replacement attempt |
| `GET /conflicts/{id}/diff?limit=…&cursor=…` | Bounded three-sided path metadata, including binary digests/sizes |
| `GET /conflicts/{id}/trees/{side}` | Exact retained manifest for `base`, `current` or `incoming` |
| `GET /conflicts/{id}/trees/{side}/files/{digest}` | Verified file declared by that exact authorized tree |
| `GET /resolution-operations?key=…` | Immutable result accepted under the current user's original key |

`base` means the original materialized session snapshot, `current` the stored publication comparison,
and `incoming` the complete finalization. The snapshot's initial directory checkpoint need not be its
materialized base. No endpoint accepts an arbitrary owner, comparison digest or host path. Export
verifies bytes into a private spool and releases the database transaction before streaming.

## Custom content and choices

Under `/conflicts/{id}`, `POST /uploads` accepts `idempotency_key` and a complete `manifest`.
`GET /uploads/{upload_id}` resumes inspection; `PUT /uploads/{upload_id}/files/{digest}` verifies a
declared file; `POST /uploads/{upload_id}/complete` saves the complete private state tree. Internal
upload keys bind the publication and hash the client key. Another conflict's, package's or
finalization's upload cannot substitute. New uploads require an active conflict; existing uploads
remain inspectable/completable after its status changes. Completion does not save a choice or publish.

`POST /conflicts/{id}/resolve` accepts `idempotency_key`, `expected_revision`, strict `dry_run`
(default false) and one `choice`. A choice uses exactly one of `use=current/incoming`,
`file_tree_digest`, or `directory_tree_digest`, with a conflict `path`, a complete connected `unit`,
or no selector for the whole directory. File choices require a path and exactly one ordinary file;
directory choices forbid a path. Opaque/database link-connected state cannot be split into files.
Custom trees are authorized as complete same-user state content and verified from actual bytes.

Ordinary path choices preserve uncontested changes. Structural choices replace the whole selected
subtree. Selecting an incoming file after directory deletion restores its required parents without
resurrecting unselected siblings. The final complete manifest must pass dependency/type/cycle
validation; incomplete or invalid combinations return no publishable partial tree.

## Atomic command behavior

The storage user lock and a transaction savepoint protect the entire command:

1. Authorize the publication. Replay an existing user key's immutable response before inspecting
   current plan status/revision; reject reuse with a changed publication or request digest.
2. Require an active conflict and matching `expected_revision`. Load immutable inputs, saved choices,
   original source identities and fresh directory/branch preconditions.
3. Compare directory mode/epoch/head, all saved branch epochs/heads, changed source validity and
   candidate-name occupancy. Also compare a fresh current projection so an unchanged removed source
   cannot reappear from an old branch. Unrelated library generation changes alone do not invalidate it.
4. On a stale target, dry-run reports the reason without saving anything. A real command supersedes
   the attempt and recomputes from the same original input. It returns a replacement ID and receipt;
   it neither applies the submitted choice nor transfers old choices to the new attempt. Invalid
   original epochs/sources detach the whole input.
5. Otherwise replace overlapping selectors while preserving independent choices. Incomplete or
   invalid combined dependencies save a pending plan. Dry-run saves no plan, choice, receipt or head.
6. Validate complete results against each branch's own current checkpoint and original snapshot
   revision/epoch/source, including branches unchanged by the original incoming submission. Full
   incoming/custom choices cannot revive pre-reset or removed sources. Custom results cannot change
   unexposed current members.
7. An occupied candidate name requires an explicit covering `use=current` choice and unchanged
   current subtree. Equal bytes do not authorize identity takeover. Incoming/custom takeover fails;
   source removal followed by recomputation is a separate operation. Valid custom new skills receive
   account-local candidate identities from a retained complete source checkpoint.
8. Verify actual content and quotas, then publish complete directory/branch heads and eligible local
   candidates through CAS. Save the plan revision, immutable operation receipt and finalization outcome
   in that same transaction. Any failure, including late directory CAS, rolls everything back.

Result status is `preview`, `pending`, `published` or `superseded`. `ready` describes whether the
preview/plan produced a complete valid result. `result_tree_digest` is absent for incomplete results;
`result_checkpoint_id` exists only after publication. The standard envelope has `committed=false`
for previews and true for accepted commands, including pending/superseded commands. This does not
mean a pending plan advanced a head. Querying an operation returns its original response even if a
later command published or replaced the plan.

## Persistence and evidence

Migration 0032 retains owner/account/publication-bound monotonic plans, independent selector rows
with same-user custom-tree foreign keys and immutable user-keyed receipts. Inputs and custom trees
remain referenced; history does not cascade-delete. Downgrade refuses when resolution history exists.
No additional migration is required for these API/service changes.

Tests cover real user authentication and device/Node/other-user rejection, bounded account/cursor
isolation, scoped uploads, corrupt bytes, exact exports, partial plans, revision races, dry-run,
immutable replay, custom files/directories/new local identities, invalid combined link cycles,
source identity collisions, unchanged reset/removed branches, unexposed members, stale recomputation
and late-CAS rollback. Service/API/publication cases pass on SQLite and migrated PostgreSQL 17.

State migration conflicts and retention remain separate implementation work. Explicit reset/restore
now marks affected account conflicts superseded with a state-reset/restore reason. A later resolve
may recompute that original attempt without applying or transferring choices.
Node `/publish` retries continue returning the retained attempt without executing a user choice.
Managed public admission and backend capability advertisement remain gated pending runtime wiring.

## Source-scoped conflict history

`GET /skills/state/conflicts` accepts optional `skill` (name or stable source UUID) alongside the
existing account/limit/cursor fields. Omitting it preserves the original account-wide behavior.
The state query service resolves library/local ambiguity and ownership. The repository applies an
EXISTS predicate over saved publication branches and their exact owner/account state before LIMIT;
it includes unchanged observed members because a conflict blocks the complete directory publication.
The same predicate verifies cursor membership. Skill filtering never narrows the conflict's actual
account-directory scope, and does not infer membership from today's name or effective revision.

## Previewing custom content before upload

`POST /api/v1/skills/state/conflicts/{publication_id}/content-preview` provides a separate permanently
read-only path for `resolve --file/--directory --dry-run`. It accepts `expected_revision`, one custom
`choice`, and its exact `manifest`; the choice digest must equal the canonical manifest digest. It
accepts no file bytes, command key or mutation flag. The user/conflict is authorized before a bounded
64 MiB body is read; the read transaction is released during network reception. The service rechecks
ownership, active status, plan revision and saved heads afterward. Parsing and pure merge computation
run off the asynchronous executor thread.

The response has status=preview, committed=false and no operation ID. Its data includes the original
conflict/account/plan identity, proposed digest, combined in-memory choices, remaining conflicts and
complete candidate digest/directory changes when available. `metadata_only=true`,
`content_verified=false` and `ready_to_publish=false` are fixed facts. `candidate_complete` describes
only the pure manifest calculation. Custom bytes, source authorization, quota admission and exact
publication preconditions remain pending checks. Unknown file digests do not grant content access.
No upload, stored tree, content reference, plan, operation receipt or checkpoint is created.

Saved choices retain their original authorization; only overlapping choices are replaced in memory.
Stale comparisons require explicit recomputation through the existing resolve protocol and are never
reinterpreted as fresh previews. The ordinary resolve request schema does not accept a manifest.
After confirmation, a client must complete the scoped content upload and obtain the existing fully
verified dry-run result, compare its candidate to the reviewed preview, and only then submit the exact
resolution request. This endpoint never weakens actual content or publication verification.

The CLI now consumes publication resolution through `skill state resolve ID`: it previews one saved
side or one exact local file/directory, confirms once, and journals the original request only after
custom content has been scoped-uploaded and the verified candidate agrees with the metadata review.
Partial choices return committed pending receipts without publishing a checkpoint. Publication,
plan CAS and stale supersession remain Server-authoritative. Staging preserves runtime data, including
Git metadata and LFS-looking text; it does not apply installation-package exclusions.

GET `/api/v1/skills/state/resolution-operations/{operation_id}` adds authenticated owner-only ID lookup
alongside the existing `?key=` route. Both return the exact original `SkillResolutionView`, even after
later choices publish the conflict. Neither route executes or recomputes a plan. CLI status falls
through operation domains only after explicit `OPERATION_NOT_FOUND`; `--last` uses its retained type
and key. No new operation table, migration or runtime capability is introduced.
