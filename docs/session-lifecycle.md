# Session lifecycle capability

The additive heartbeat field `runtime_capabilities.session_lifecycle` is a map
with recognized backends `native` and `docker_sandbox`. Each supported entry is
exactly `{"supported":true,"protocol_version":1}`: boolean true and integer 1
are required, with no additional entry fields. Missing, false, malformed or
incompatible entries clear that backend's previous capability. Unknown backends
are discarded. An empty map advertises no support.

Version 1 means the installed privileged Helper persists explicit stop intent
and prevents a late physical launch of that same session, including recovery and
restart paths. It does not mean a particular process has stopped, a Skill tree
has been captured or published, or a workspace has finished synchronization.
The Node worker must obtain this capability from the currently installed Helper
over its authenticated local socket and intersect it with healthy runtime
backends. A worker version or an old successful task result cannot confer it.

The Helper must validate its private lifecycle storage, bounded no-follow access,
cross-process locking, atomic publication and persistence before reporting
support. Native support includes managed spec, launch, cancel and recovery;
Docker support does not advertise managed Skill support. Unknown or unsafe storage
means unsupported without repairing permissions or removing retained data.

The capability field itself only records normalized support from the Node.
It does not authorize a new stop API, change existing task receipts, infer current
termination from historical success, or declare stop/delete GUI work complete.
Future exact lifecycle operations must require the current matching backend
capability and their own original-task and termination evidence.

Legacy stop failure does not establish termination: explicit stop keeps the session
`stopping`, while reconciliation cleanup preserves `interrupted`. The failed task
and its original receipt remain immutable; no automatic retry is introduced.
Single and bulk history deletion lock the original sessions before checking every
associated stop/cleanup task. Pending, failed, cancelled, expired, missing or
inconsistent successful receipts block deletion. Bulk deletion validates all
candidates before releasing Skill references or deleting any history. A genuine
create failure with no stop task retains the existing deletion behavior.

These checks preserve historical successful stop receipts as compatibility data;
they do not upgrade them to physical termination proof. Managed termination and
retained-content guards remain independently required. Uncertain historical stop
tasks need a future explicit recovery contract, not receipt rewriting.

Runtime reconciliation also refreshes and locks each original session after the
history lock, rechecking its active state and absence of a managed snapshot before
enqueuing cleanup. A stale inventory read cannot revive deleted history, interrupt
a later lifecycle state or insert cleanup after deletion preflight.

The owner-scoped stop caller-key contract is available on the existing session
route. A client may submit `POST /api/v1/sessions/{session_id}/stop` with a UUID
`request_id`, schema version `1`, the reviewed session/node/backend binding, and its
intent fingerprint. The server records the exact stop task row, runtime identity,
and Skill finalization pointer in `session_stop_requests` in the same transaction
that enters `stopping`. Repeating the same key with a different session, binding,
reason, or fingerprint returns a fixed conflict and never schedules another task.
The owner can recover the immutable receipt and current task/session/save
observation with `GET /api/v1/sessions/stop-requests/{request_id}`, including after
the original session is deleted. Dynamic phases are `accepted`, `stopping`,
`stopped`, `failed`, or `deleted`; an unresolved stop remains pending or failed
according to its task evidence and is never promoted by a historical receipt.

Before accepting the first successful `stop_tool_session` result, the Server
requires `status=stopped` and an exact `session_id` match with the frozen task
payload. If a Node includes `runtime_backend` or `runtime_resource_id`, those
fields must also match the original binding; omitted optional fields remain
compatible with older Node result shapes. A mismatch returns
`SESSION_STOP_RESULT_INVALID`, leaves the task and session unresolved, and cannot
produce `process_stopped=true` in the caller-key observation.
