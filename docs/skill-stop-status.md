# Managed session stop and saving status

The original session snapshot UUID is the durable finalization operation ID, available before
upload and after display-session deletion. `GET /sessions/skill-finalizations/{operation_id}`
uses the existing session user/device authentication and restricts lookup to the authenticated
owner. It is a metadata-only read and remains available when new managed sessions are disabled.
A single database statement reads the original snapshot, termination, incoming finalization,
latest publication and optional display session; it never reads a cached stop-task result.

States are `awaiting_node`, `capture_pending`, `local_durable`, `upload_pending`, `persisted`,
`persisted_unclean`, `published`, `conflicted`, `detached`, and `superseded`.
`awaiting_node` makes no claim of local durability. Only an exact termination observation
confirms process quiescence. Persistence and publication are separate; an unclean capture never
implies automatic inheritance. Content retirement remains separately visible, and neither a
historical publication nor session deletion implies currently available content.

Session GET and stop responses include nullable `skill_finalization_operation_id`. Legacy
sessions return null. Status reports both the current display-session status (or `deleted`) and
`process_stopped`, which is based on the retained exact termination receipt. Stop replay and
missing runtime metadata do not manufacture saving completion.

New stop tasks carry `skill_finalization` with the original snapshot, preparation task, user and
account UUIDs. Native Node dispatch handles any such marker outside permanent generic failure
caching. After writer stop and full frozen-input observation, it attempts saving for up to 10 seconds.
The background inventory loop and immediate attempt share one process-owned ledger and cancellable
transfer gate. Admission ownership is released before waiting for that gate; network waits never
hold a runtime alive. The task result retains only immutable stopped identity/digest/classification.

Server completion independently locates the original snapshot even if the task marker is missing.
It accepts only the exact six-field result matching an already committed termination observation.
A missing observation returns STATE_PENDING; failed/malformed managed stop reports cannot consume
the task or change session state. The separate termination request remains responsible for durable
quiescence and binding revocation. Exact task-result replay is side-effect free, and unclean results
preserve `interrupted`. This does not add a managed Docker capability.

`fclaude stop SESSION --timeout 60` waits only for managed sessions. It prints the operation ID
before waiting; `fclaude stop-status OPERATION_ID [--wait] [--timeout 60]` uses the same device token
and remains usable after session deletion. Saving waits return 3 on timeout, 130 on Ctrl+C, and 1
for conflicted/detached/superseded outcomes requiring review. Published returns 0; a plain pending
status read also returns 0. HTTP/identity failures return 1. Legacy stop stays an immediate request.

`capture_pending` confirms stopped writers through the independent `/capture-pending` receipt.
It includes a finite `capture_error`, no content digest and no durability claim. Background capture
retries continue without an upload journal. Explicit stop can complete with a null incoming digest
only after this Server observation commits; its original six-field result remains immutable even
after full capture upgrades the termination row. The CLI ends waiting with exit 1 and prints the
original snapshot export command. A later read may advance to local durability or publication.

The opt-in CLI lifecycle acceptance drives current fclaude commands with an isolated registered
device, real Mutagen workspace synchronization and authenticated SSH attach. A synthetic runtime
writes deterministic state, explicit stop waits for publication, and a new session reads the saved
state after daemon restart. Model inference remains a separate acceptance requirement.

This acceptance passed on 2026-09-25 (30.47 seconds), including effective selection and a status
query against the original publication after session deletion. It exposed launcher argument routing:
global options before a subcommand previously sent `stop` to the default run/attach path. The CLI
now prioritizes explicit subcommands while preserving direct and `--`-delimited Claude arguments;
a regression failed before the fix. No lifecycle receipt is injected by this test.
