# Deployment preparation and task binding

This boundary implements exact reservation and authorization. Explicitly compatible Native accounts
are accepted as pending and ordinary authenticated polling schedules bounded original targets, as
specified in `docs/skill-deployment-scheduling.md`. The Node does not advertise this capability yet.
Dedicated authenticated content/result routes and the Node worker consume only exact reserved tasks.

The caller selects an exact original operation/account/attempt. Under the user storage lock and a
retention savepoint, reservation validates the entire saved plan and attempt history, rejects
supersession, and checks the original active account binding and current effective selection. It
requires the feature gate, a fresh compatible Native backend and the additional strict integer
`skill_manager.native.deployment_protocol_version=1`. The current Node does not advertise it.
Missing or incompatible capability is an error, never evidence of readiness.

Only pending attempts can create work. Existing takeover admission may return its original pending
receipt; ordinary migration conflicts are committed as `needs_resolution` without creating a
deployment task. Complete account materialization uses the same composition and format checks as
session snapshots, preserving root data and enabled library/local branches. It creates a separate
directory checkpoint with normalized member references. This checkpoint is not the account head,
a session, or an effective-use observation. No running session files are changed.

Migration 0050 adds `skill_deployment_tasks`. Each row composite-binds the user, original operation,
account and exact attempt, the original node and exact NodeTask row, and an owned directory
checkpoint. It also preserves the resolved execution plan and full content digest. Original acceptance digests stay unchanged;
`docs/skill-deployment-discovery.md` describes the explicit first-takeover supplement. One task per attempt is
unique; retries append tasks while reusing the first reserved directory input, even after unrelated
head progress. Directory or member epoch invalidation rejects reuse; it never silently refreshes the
input. A saved failed task is not retroactively completed. Downgrade refuses any recorded binding.

Content authorization identifies the saved binding before acquiring the original user's storage
lock, then rereads all authority. Every request requires the exact task record and poll attempt,
active lease, canonical payload, original pending/running attempt, current matching configuration,
valid directory/member epochs and fresh capability. A stale generation alone does not invalidate an
unchanged plan; supersession and actual selection changes do. Knowledge of a digest grants nothing.
Dedicated Node content and lease routes invoke this check on every request.

Generic Node completion and failure reject any durable deployment binding (including tampered task
types/payloads) and unbound deployment task types. The dedicated validated success protocol in
`docs/skill-deployment-results.md` atomically confirms the original preparation. A local preparation
receipt alone never writes Server `ready`.

Retention protects the complete reserved directory and its exact members while its attempt is
pending/running/needs_resolution or remains retryable on an unsuperseded operation. Supersession does
not release active tasks: it is neither cancellation nor proof that local work stopped. Terminal
metadata remains after normal explicit checkpoint retirement, without a permanent tree foreign key.
Checkpoint/member history dependencies and existing release clocks govern subsequent collection.

Node task lifecycle is checked independently of attempt projection during retention: a still
pending/leased/running/expired task protects its input even if its attempt was prematurely marked
failed or superseded. Expiry proves neither local drain nor permission for a successor to execute.
Retry reservation requires preceding tasks to have actually reached failed/cancelled
control-plane states and retain exact accepted dedicated drain results. Status flags alone do not
authorize a successor. Worker durable failure/cancellation recovery is implemented.
Disabled owners cannot obtain new deployment content authorization. Invalid optional
capability versions are removed during heartbeat normalization so Python's boolean/integer equality
cannot preserve a prior deployment grant in the ORM.

## Authenticated deployment content transport

`GET /api/v1/node/skill-deployments/{attempt_id}` returns the complete original plan, directory
manifest and exact normalized members. `task_id` and positive `lease_attempt` query parameters are
mandatory. The response status is `prepared_input`, committed=false; this is content availability,
not Node readiness. The encoded envelope is limited to 64 MiB.

`GET` on the same resource's `/files/{digest}` serves only file members of that saved directory.
It first authorizes, commits the short lock transaction, verifies the complete private file in
bounded disk-backed staging, then reauthorizes the same node/task/attempt/poll binding before sending
bytes. Responses require exact Content-Length, digest ETag and application/octet-stream. Known
foreign/same-user digests outside the manifest are not readable. Corrupt or missing retained bytes
never produce a successful partial response. Streams close staging on error, cancellation or finish.

`POST .../{attempt_id}/lease?task_id=...` accepts only a strict integer `lease_attempt` body. It renews
only that still-active poll attempt, at most 300 seconds, returning the complete original binding,
server time, deadline and renewal interval. Expiry, replacement, disabled owners, changed state epochs
or revoked capability cannot be revived by renewal. Reads and renewals never create tasks, prepare
new inputs, advance heads, record effective use or mark a deployment ready.

These routes retain the Node credential and manager feature gates. They expose transport for already
reserved tasks. Ordinary polling now invokes reservation, and the dedicated worker/result success
chain is implemented. Full runtime acceptance remains pending.
