# Deployment revocation and drained termination

Deployment failure requires two distinct durable observations. A Server termination intent revokes
all further content, lease and success authorization for the exact original attempt. Only a later
matching permanent Helper drain can end the task and target. Neither timeout, supersession nor a
local error substitutes for either step. The original success and first revocation serialize under
the same user-then-task locks: an already successful task cannot receive a drain directive.

Migration 0051 adds `skill_deployment_terminations`, one immutable intent per exact deployment binding.
The attempt primary key references `skill_deployment_tasks`; the intent UUID is globally unique.
It stores the requesting poll, original bounded failure code and the Server-selected failed or
superseded outcome. No file bytes, credentials or paths are stored. Superseded is selected only from
the original operation's actual replacement marker. Retryability derives from the existing bounded
retryable failure codes and is false for superseded outcomes. No existing task is backfilled.
Downgrade refuses any intent before dropping the table.

The authenticated original Node may request an intent under its current original poll, including
an expired lease, since the operation only revokes authority. It may read an existing intent without
obtaining a new lease. Repeating the exact request recovers the same intent; changed requests conflict.
Active original owner, canonical task binding and original current target are mandatory. New feature
or capability gates do not prevent revocation. Intent publication leaves task/attempt nonterminal and
keeps original input roots. Subsequent polls may rediscover the task but cannot authorize preparation.

Confirmation submits the exact intent plus schema-1 permanent Helper drain with matching complete
binding. A single retention savepoint saves one NodeTaskResult, clears the task lease, records
failed/cancelled task state and failed/superseded original attempt, and updates the projection and
release clocks. Retryable failures continue protecting the original input. The result also records
the final observed poll; terminal replay must preserve that poll and an absent lease. Exact replay
and read-only inspection require original Node/owner identity but no current content or capability.
They do not reacquire references or overwrite a successor attempt. Generic task results remain denied.

Server directives do not prove local exclusion. Helper drain does not prove Server acceptance or
permit local deletion. Worker orchestration and ordinary scheduling must retain those distinctions;
capability advertisement remains disabled until the complete lifecycle is integrated and verified.

## Authenticated Node routes

All paths begin `/api/v1/node/skill-deployments/{attempt_id}` and require the original Node token
plus query `task_id=<original NodeTask database UUID>`:

| Method and suffix | Body | Successful envelope |
| --- | --- | --- |
| `POST /termination` | `{lease_attempt,error_code}` | `drain_required`, committed=true, data is the original intent |
| `GET /termination` | None | `observed`, committed=false, data is `{intent:<original or null>}` |
| `POST /termination/result` | `{intent,drain}` | `confirmed`, committed=true |
| `POST /termination/result/inspect` | Same original `{intent,drain}` | `observed`, committed=false |

The intent contains `version=1`, `intent_id`, complete original `binding`, exact `request`, `outcome`,
`error_code` and `retryable`. The request error code is one of NODE_UNAVAILABLE, TRANSFER_FAILED,
QUOTA_EXCEEDED, DEPLOYMENT_INTERRUPTED, AUTHORIZATION_DENIED, SKILL_MANAGER_UNSUPPORTED,
DEPLOYMENT_INPUT_INVALID or OPERATION_SUPERSEDED. Only the first four permit failed-target retry.
An actual replacing operation makes the outcome superseded, error OPERATION_SUPERSEDED and
retryable=false, retaining the original request. Claiming OPERATION_SUPERSEDED without an actual
replacement conflicts. The public original operation continues reporting its first replacement ID.

Drain contains `version=1`, complete `binding` and nonzero `helper_receipt_id`; no file bytes or
Helper paths are accepted. Confirmation/inspection data contains `result` (exact submitted body),
`accepted`, `current_lease_attempt` and `task_status`. A successful failed-target confirmation has
failed task status; superseded has cancelled task status. Both use the existing NodeTaskResult
category failed, whose schema permits only succeeded/failed; the complete dedicated result retains
the actual superseded classification. The saved result JSON is `{result,lease_attempt}`, with the
last field pinning the poll at terminal commit. Later polls before confirmation never reopen a
revoked attempt. Terminal replay rejects changed poll, nonempty lease or altered result metadata.

Retry reservation now validates the predecessor's exact accepted dedicated drain result; a task
status flag alone is insufficient. A failed proposal cannot authorize an overlapping successor.
The typed Go HTTP client validates original identities, requested classification, canonical fields,
integer bounds and exact result observations. It does not invoke Helper or automatically retry
uncertain POSTs. Worker termination dispatch and its independent durable recovery remain pending.
