# Reviewed runtime recovery requests (schema 1)

This additive GUI/CLI contract reuses the existing runtime recovery task, leases and results.
Legacy `/runtime-migration/recover` routes and Node protocols remain unchanged. No new tables
or server fingerprint are introduced. CLI plans bind actor, Server/context, action and the exact
original projection locally; inspection is observation, never authorization or proof of recovery.

## Authentication and routes

All four routes accept only active `token_type=user` credentials with `user_device_id=null`,
validate active users, and suppress device last-seen updates even on rejection. Device and Node
credentials cannot gain GUI administrator access. Existing authentication dependencies are unchanged.

| Method/path (under `/api/v1`) | Authentication | `data` |
| --- | --- | --- |
| GET `/runtime-recovery/authority` | User token | `{schema_version:1,actor_user_id,admin:boolean}`; ordinary users receive 200/false |
| GET `/tool-accounts/{account}/runtime-migration/recovery-inspection?original_task_id=...` | User administrator | `{schema_version:1,original,original_status,account_status}` |
| POST `/tool-accounts/{account}/runtime-migration/recovery-requests` | User administrator | Request receipt below; HTTP 200 for acceptance and replay |
| GET `/tool-accounts/{account}/runtime-migration/recovery-requests/{request_id}` | User administrator | Original request receipt below |

Inspection selects an explicit migration task belonging to the requested current account owner.
It never returns raw payload/profile/result, host paths, arbitrary error text or an eligibility
claim. Reads create no task, audit or storage row. Missing/mismatched resources return 404.

## Strict wire fields

`OriginalProjection` has exactly these required, non-null fields:

```json
{
  "task_id": "migrate_tool_account_runtime:11111111-2222-4333-8444-555555555555:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "task_record_id": "22222222-3333-4444-8555-666666666666",
  "user_id": "33333333-4444-4555-8666-777777777777",
  "tool_account_id": "11111111-2222-4333-8444-555555555555",
  "node_id": "44444444-5555-4666-8777-888888888888",
  "tool_type": "claude",
  "source_runtime_backend": "docker_sandbox",
  "target_runtime_backend": "native"
}
```

`user_id` means the account owner, not the administrator. UUIDs use canonical lowercase,
hyphenated text. `task_id` is a bounded canonical migration logical ID, matching its account.
Backends are `native|docker_sandbox`, and source/target must differ.

POST body is exactly `{schema_version:1,request_id,action,expected_original}`. Version is a strict
integer, request ID is a canonical nonzero UUID, and action is explicitly
`verify_target|verify_source|repair_source`. Unknown fields, missing/null fields and coercion are
rejected with fixed `COMMON_VALIDATION_ERROR` (422), without echoing request input. Path account
and `expected_original.tool_account_id` must agree. Request bodies are bounded to 8 KiB before
JSON parsing, including chunked bodies; overflow returns `RUNTIME_RECOVERY_REQUEST_TOO_LARGE` (413).

POST/GET receipt is exactly `{schema_version:1,request_id,action,binding,status}` inside `data`.
`binding` preserves the existing complete v1/v2/v3 structure:
`version,task_id,task_record_id,original_task_id,original_task_record_id,node_id,user_id,
tool_account_id,tool_type,source_runtime_backend,target_runtime_backend`, plus `action` only for
v2 (`verify_source`) and v3 (`repair_source`). V1 omits binding.action; the receipt's outer action
is always `verify_target`. All fields are required except that mandatory v1 omission.
The outer API envelope retains the usual tracing `request_id`, distinct from `data.request_id`.

Task status is `pending|leased|running|succeeded|failed|cancelled|expired`.
Account status is `binding_requested|binding_session_starting|binding_waiting_user_login|
binding_verifying|active|expired|disabled|failed|node_unavailable|migrating`.
Unknown stored statuses or malformed safe projections fail closed with a fixed conflict.

## Acceptance and recovery ordering

Keys retain existing `(account_id,request_id)` scope, not actor scope. Any current user-token
administrator may access management history; the local CLI journal enforces its actor/context.

The existing user writer lock protects new acceptance. Server derives the complete original
projection from the original task record and compares it to `expected_original` before existing
current-state admission and task creation. A changed original projection, only when no saved
request exists, returns 409 `RUNTIME_MIGRATION_RECOVERY_REVIEW_CHANGED`. Missing original records
also invalidate an unsaved review. Current admission conflicts retain
`RUNTIME_MIGRATION_RECOVERY_CONFLICT`; neither error creates a task or audit.

For a saved key, validate its stored binding, compare action and expected projection against that
binding, and return the original binding with that task's current status. Never run inspection
or compare the old receipt against the current backend/node/profile/newer migration. A different
intent with the same key returns 409 `RUNTIME_MIGRATION_RECOVERY_CONFLICT`, never review-changed.
Current administrator authorization and current account ownership/active owner checks still apply.
GET never creates or resubmits work; 404 is not proof that a concurrent POST cannot commit.
Acceptance is not task success. Node authorization, lease and result validation remain authoritative.
