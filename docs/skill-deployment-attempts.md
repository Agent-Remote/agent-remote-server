# Durable deployment attempts and exact retry acceptance

Migration 0049 adds `skill_operations.attempts_version` and two owner-scoped tables. Historical
operations retain NULL; no attempt is reconstructed from current configuration. Their historical
retention obligations remain, but status does not advertise the new retry authority without a
verified attempt chain. New library
acceptance uses version 1 and appends attempt 1 for each original target in its existing savepoint.
Unbound targets start stored; explicitly compatible bound targets start pending and incompatible
targets start unsupported, following `docs/skill-deployment-scheduling.md`. Recording an attempt does
not advertise runtime capability or prove materialization.

`skill_deployment_attempts` binds each UUID and positive sequence to the original operation/account
plan through composite foreign keys. A successor names the exact preceding attempt of that same
target, has sequence predecessor+1, and preserves its original plan digest. Only a failed attempt
with a classified transient error is retryable. A terminal attempt is retained as history; retry
appends a pending attempt and never rewrites the predecessor. Current target metadata in the
operation result (attempt ID, number, per-target retryability and error code) must agree with the
complete, gap-free attempt chain. Missing, mismatched or
unclassified history fails status and retention validation rather than appearing ready.

`skill_deployment_retries` records the authenticated owner, original operation, request key and
canonical request digest in the same transaction as all new attempts. Retry requires the original
operation generation and exact current failed attempt IDs. A repeated identical key does not
append another attempt, even after later progress. A changed request under the key is rejected.
Successful/stored targets, unsupported nodes, permission errors, unresolved conflicts, superseded
operations and active attempts cannot be selected. The complete selected set is checked before any
new attempt is saved. Retry never reads Git/local sources, uploads new content, changes configuration,
or replaces original plan input with current rules.

Original input plans and current account ownership/binding/selection are checked under the user
storage lock. New unrelated generations do not by themselves invalidate a retry; a changed effective
selection or binding does. Pending/retryable attempts keep their original package/local revisions
rooted through the operation, and all transitions share existing retention clock transactions.
Superseding an operation never releases an independently pending/running/conflicted target: the
operation remains a content root until every active target has ended. Supersession still prevents
new user retries, independently of that retention duty.

`POST /api/v1/skills/operations/{operation_id}/retries` accepts the exact retry request and
commits before returning the original operation envelope. `GET` on that same path with `key`
recovers only an existing retry receipt for the authenticated owner and original operation. A
missing receipt returns `OPERATION_NOT_FOUND`, even if that operation exists. Library acceptance
keys and retry keys have separate lookup namespaces. GET never appends attempts or renews tasks.
The existing live user-token and feature-gate requirements apply to both routes.

The API/CLI retry surface and actual Node deployment scheduling consume this ledger separately.
Ordinary polling now schedules bounded pending targets; absent or incompatible backend reports
remain explicitly unsupported. Offline known-compatible targets retain pending configuration. Downgrade refuses recorded attempt
or retry history before removing any schema. No content bytes or credentials enter these tables.

## Configuration supersession

A changed library command, after saving its complete original target plans and initial attempts,
checks older unfinished operations for the same affected accounts in the same user transaction.
Unfinished means pending/running, an unresolved state conflict, or a classified retryable failure;
completed nonretryable failures (including unsupported targets) remain historical observations.
Comparison uses the two saved effective plans, ignoring only operation identity and generation.
A newer generation alone does not replace an account pinned to an unchanged selection. Staging,
no-op commands, other accounts/tools, successful targets and unknown legacy attempt histories do
not create inferred replacements. A changed unfinished target marks its parent operation superseded
with the first replacing operation ID. Later changes do not rewrite that original replacement link.

Replacement is operation authority, separate from target execution evidence. Original plans, failed
predecessors and successful/current attempt observations remain intact. Pending/running/conflicted
targets still root their original content until independently ended; supersession itself cannot
prove writer drain, task cancellation or runtime cleanup. New retries are rejected; an already
accepted retry key still resolves the historical operation. Status and retention require a real
same-owner, higher-generation replacement with saved changed selection evidence, and refuse missing,
foreign, cyclic/backward or unrelated replacement references. No schema change is required.

The replacement marker, new command, generation and retention clocks share the existing savepoint.
Late target completion cannot turn a superseded operation ready. This is the configuration fence
for later task dispatch; dispatch authorization, cancellation acknowledgement and Node progress
remain separate required work.
