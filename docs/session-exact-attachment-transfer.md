# Exact Claude session attachment transfer

This is the v1 contract for file and image bytes sent from an exact
`sessions.attach` terminal. The Server admission and cleanup routes are now
implemented, and the Node gateway and CLI now implement the same versioned
contract. The capability remains a release-gated opt-in until the real
Server/Node/SSH forced-command acceptance test passes. Exact sessions remain
text-only when that evidence is absent; legacy attachment requests are never
accepted as a fallback.

Before issuing a transfer lease, the Server must recheck the same identity used
by exact terminal attachment:

```text
node_id, session_id, expected_device_id, ssh_key_id, lease_id
```

The authenticated device must own the session, the session must still be a
running Claude session on that Node, the active SSH key must equal
`ssh_key_id`, and the installed gateway must advertise
`session_attachment_transfer` protocol version 1. The lease is opaque,
single-purpose, byte-limited and expires independently of the attach advisory
TTL. Account paths and credentials are never returned to the client.

The admission route is
`POST /api/v1/node-api/attach/exact/attachments/verify` and uses strict
request/response schemas. A legacy attach response, an old Node capability or a
missing key identity cannot authorize transfer. The Node supplies a fresh
canonical `lease_id`; the response echoes the complete identity and returns only
an opaque `runtime_scope`, backend/resource identity, expiry and byte/entry
limits. It never returns an account path. Cleanup receipts are submitted to
`POST /api/v1/node-api/attach/exact/attachments/cleanup` with the same identity.
Admission and cleanup recheck session lifecycle and device/key ownership;
session deletion or device revocation invalidates outstanding leases while
retaining immutable identity snapshots and cleanup receipts.

The Node request uses the actual forced-command `device_id`; the client frames
continue to call the same value `expected_device_id` because they are checked
against the already verified Node lease. The Node returns a public cleanup
receipt bound to the original session, device, key and lease. The only successful terminal state is `cleaned`;
`pending`, `unknown` and a lost response require explicit recovery and remain
visible to the client. The Server must retain enough immutable receipt metadata
to prevent a new lease from being mistaken for completion of an old one.

Required acceptance cases include wrong device/key/session, lease replay and
expiry, changed runtime account, path traversal or links, archive checksum and
size mismatch, concurrent leases, session deletion during transfer, and lost
cleanup responses. No route may accept a client-selected account path.
