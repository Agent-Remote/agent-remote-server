# Exact Claude session attachment

`POST /api/v1/sessions/{session_id}/attach/exact` accepts exactly
`{"expected_device_id":"<UUID>"}`. Before any mutation, the authenticated active
device must equal that ID. The session must belong to the active user, have
`tool_type=claude`, be running or active, have a terminal and an available node,
and have no blocking workspace sync state. Native and Docker Sandbox are supported.
Both POST and GET require the installed gateway capability
`runtime_capabilities.session_attach={"supported":true,"protocol_version":1}`;
the boolean and integer types are exact. No legacy fallback is allowed.

The strict version 1 data contains `version`, `tool_type` (literal `claude`),
`session_id`, `device_id`, `node_id`, `ssh_host`, `ssh_port`, `ssh_user`,
`tmux_session_name`, `forward_ssh_agent`, `authorization_task_id`,
`authorization_task_record_id` (UUID), `authorization_task_status`, and `expires_in`
(300, an advisory window, not a bearer authorization). Task status is exactly one
of pending, leased, running, succeeded, failed, cancelled or expired.

POST reuses the deterministic device/node/current-key-set SSH synchronization task.
It never resets a failed task or changes a mismatched existing payload. Only POST
may create the missing task and write the authorization audit. After one POST,
poll only `GET /api/v1/sessions/{session_id}/attach/exact/{authorization_task_id}`
with required queries `expected_device_id` and `expected_task_record_id`. GET
rechecks current user, device, session, node, capability, keys, forwarding and
the task's exact row UUID, logical ID, node, type and entire key payload. It writes
no audit, last-seen timestamp, task reset or task. An unknown POST outcome must
not trigger automatic POST retry or session creation.

The client constructs the fixed command
`agent-remote-attach --session <UUID> --expected-device <UUID>`. The installed
gateway compares expected device with the trusted authorized_keys forced-command
device, and forwards its actual SSH key ID to the new verification route.

`POST /api/v1/node-api/attach/exact/verify` accepts exactly four required UUIDs:
`node_id`, `session_id`, `device_id`, `ssh_key_id`. The authenticated Node must
match; the session, current active owner/device/key, capability, lifecycle and
workspace sync state are rechecked. The strict version 1 response data contains
`version`, `tool_type=claude`, exact echoes of all four UUIDs,
`tmux_session_name`, `container_id` (required nullable string), `runtime_backend`
(`native` or `docker_sandbox`), `runtime_resource_id` (required nullable string),
and `forward_ssh_agent` (required boolean). The standard response envelope retains
`request_id`. Node rejects missing, malformed or mismatched echoes before runtime
dispatch. Current credential policy controls forwarding on each connection.

These additive routes do not change old session attach or verify contracts, start
sessions, authorize other tools, prove process termination, or make tmux multi-attach
independent. Runtime acceptance over real SSH remains separate from API tests.
