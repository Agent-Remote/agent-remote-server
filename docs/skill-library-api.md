# Private skill library API

The library and package APIs are enabled by default. Set `SKILL_MANAGER_ENABLED=false` to
explicitly disable them. Existing deployment environments containing `false` retain that override;
change it to `true` when migrating the old installation default. Runtime deployment still requires
a fresh compatible Node capability report and account takeover; enabling the API does not enroll
accounts or certify model loading. Existing sessions retain their original runtime.

Every management request uses a current **user** token. Device tokens and Node tokens cannot
access these routes. Source URLs contain no credentials; Git is fetched by the local CLI. A local
source locator is a SHA-256 identity chosen by the client, never a reusable remote host path.

All mutations carry `idempotency_key` and `expected_generation`. The Server takes the user's
storage lock, checks an existing idempotency result first, then checks generation. Same key and
request return the original operation even after later changes; same key and different request
fail. A failed multi-item add rolls back all library changes. Successfully committed configuration
and deployment readiness are represented separately.

Migration `0047_skill_deployment_plans` adds original account configuration plans. New target
receipts include `plan_digest`; historical receipts may return null. The plan records the original
binding and the complete effective source/version selection, including disabled sources and account
pins. Replays and status verify saved plans without recomputing current rules. Retryable terminal
operations retain their plan's exact content references. This is not a runtime snapshot or evidence
of deployment; the ended-operation retry endpoint and execution attempts are still pending. See
[plan persistence](skill-deployment-plans.md).

| Method and route under `/api/v1/skills` | Request / behavior |
| --- | --- |
| `GET /` | Current user library and generation; `tool` explains a tool; account queries require `account_id` plus `effective=true` |
| `GET /installations/{name-or-id}` | Library revisions/overrides or explicitly account-scoped local detail; stable IDs also resolve archived sources |
| `POST /installations` | `SkillAddRequest`: all selected items and initial scope, atomically |
| `POST /updates` | `SkillUpdateRequest`: candidate content, optional stage and explicit tracking switch |
| `POST /rules` | `SkillRuleRequest`: enable, disable, pin, unpin or field-specific inherit |
| `POST /rollbacks` | `SkillRollbackRequest`: explicit revision or previous distinct activated revision |
| `POST /removals` | `SkillRemoveRequest`: archive the current installation epoch |
| `GET /operations?key=...` | Recover a lost acceptance response from the saved idempotency key, without replaying a command |
| `GET /operations/{id}` | Original current-user operation and per-account readiness |
| `/content/...` | Separate [content upload/download protocol](skill-content-storage.md) |

Request and response schemas are generated in OpenAPI from the typed models. The common result
has `schema_version`, `operation_id`, `status`, `committed`, `retryable`, `data` and `errors`.
Business failures use stable error codes, with authorized differences for generation conflicts
and source-layout changes. A rule query describes configuration, sets `model_loaded=false`, and
marks project discovery `not_inspected`; it does not claim that the tool actually loaded a skill.

The field order is account → tool → user independently for enabled and revision. An unrestricted
installation persists a user default, not an expansion of today's tool list. Explicit scope
creates enabled overrides over a disabled user default. Repeated add never resets existing rules.
`disable --all-scopes` clears only enabled overrides; pins remain. A deleted account is subject
to all existing lifecycle guards before its own override rows can be removed.

Each revision retains its initial provenance and exact content identity. A later observation of
identical content is a separate row. Stage does not change default tracking or activate history.
Rollback selects actual history and fixes tracking; a new explicit ref is required to resume Git
tracking. Known moved tags fail with `SOURCE_DRIFT`. Reinstalling the same source advances its
installation epoch while preserving identity and rules; a different source with the same name
receives a new ID after the old source is archived. Runtime checkpoint inheritance across epochs
is still pending the state layer and is not claimed by this API implementation.

Migrations `0026_skill_content_storage` and `0027_skill_library` must be applied before enabling
these routes. Backups include both database metadata and the private content volume. Library
migration downgrade removes the new metadata and must follow explicit export, not be used as an
implicit data downgrade.

## Account-local queries and rules

Account-scoped lists expose `local_items` separately, including disabled active local sources.
Local detail uses `origin=account_local`, account/checkpoint identity and state revisions, without
inventing Git provenance. Only an explicit matching account scope can read or change a local
source. Names shared by library/local sources are rejected with `SKILL_SOURCE_CONFLICT`; stable
IDs select the exact source. Unqualified library names also reject owned local collisions.

Local enable/disable/inherit(enabled or all) reuse the user lock, generation CAS and original
operation replay protocol. Inherit restores enabled=true. Pins, revision inheritance, removal and
package rollback do not apply to local sources. Changes target only their account and never alter
state heads, state/directory epochs or existing snapshots. Runtime readiness remains separate.
