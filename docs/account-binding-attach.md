# Exact account binding attachment

`POST /api/v1/tool-accounts/{account_id}/bind/attach` accepts exactly
`{"expected_binding_session_id": "<complete binding attempt ID>"}`. An active device
token, account ownership, an attachable current binding, the exact attempt and an
available assigned node are required. Both POST and GET require
`runtime_capabilities.binding_attach` to contain literal `supported: true` and
integer `protocol_version: 1`. Missing, malformed, false or future capabilities
return `NODE_BINDING_ATTACH_UNSUPPORTED` (409) before creating an SSH task.
Protocol v1 means both exact five-word binding-attempt dispatch and verification
of the current forced-command SSH key, including the Server key echo.
The Node reports support only after probing its configured installed SSH gateway
with the fixed `--protocol-info` argument; the Node version alone is insufficient. It synchronizes the device's active SSH keys
for either backend using the existing deterministic device/node/key-set task.
This authorizes SSH transport; it does not start or verify an account binding.

The version 1 response contains `tool_account_id`, `binding_session_id`, `node_id`,
`device_id`, `ssh_host`, `ssh_port`, `ssh_user`, `tmux_session_name`,
`authorization_task_id`, `authorization_task_status`, and `expires_in` (300).
No shell command or agent-forwarding permission is returned. The lifetime is an
advisory connection window, not a bearer grant; the Node reauthorizes every SSH connection.

After the single POST, poll only
`GET /api/v1/tool-accounts/{account_id}/bind/attach/{task_id}?expected_binding_session_id=...`.
GET rechecks owner/device/node/attempt and the original deterministic SSH task's
type and complete payload. It never creates or resets tasks. Missing tasks, changed
keys, changed generations or revoked authority fail closed. An unknown POST result
does not authorize an automatic retry; bind/start, verify and create are also not
idempotent operations. A failed original authorization task remains failed when polled.

The CLI constructs only the fixed remote argv
`agent-remote-attach --binding <account UUID> --binding-session <expected attempt>`
from validated structured data; it never parses `connect_command`. The updated Node
passes the expected attempt to `/node-api/binding-attach/verify` and also compares
the response before invoking the runtime helper. The exact flow also sends `ssh_key_id`
from the trusted authorized_keys forced-command identity, never from the original
command text. The Server checks that key is active for the device and echoes its ID;
Node requires that verified-key echo. Missing or mismatched echoes, including an older
Server ignoring the new request fields, fail closed. Older Node/gateway installations do not
support the new command and are rejected by the capability gate. Upgrade the Node
and installed SSH gateway together, then wait for a fresh successful Node report.
Clients must not fall back to unbound attachment.
The legacy three-word command remains compatible but is not used by the exact flow.

Disable means account admission is disabled and port forwards are revoked; it does
not kill existing terminals or delete login state. Pending binding/verification
results cannot reactivate a disabled account. Verification rejects disabled accounts;
an explicit new binding remains a separate user action. Account row locks serialize
disable, verification admission and binding/verification result publication.

Tests use isolated API databases and fake SSH/runtime endpoints. Real node SSH,
both runtime backends and native terminal behavior require separate runtime acceptance.
