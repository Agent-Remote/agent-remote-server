# Frozen Node snapshot export

The design permits an owner to export a stopped snapshot that has not reached Server storage,
including when state quota prevents upload. This transport uses the existing SSH forced-command
gateway and Helper retained-object descriptors. It does not upload content to bypass state quotas.

`POST /skills/state/node-exports/{snapshot_id}/authorize` requires a live user token and the user's
active device/SSH key. It binds the original snapshot, account, Node, session, preparation task,
library generation, directory epoch and initial tree digest. It can schedule only the existing
idempotent SSH-key synchronization task. Current account binding/configuration cannot substitute
another Node or snapshot. A grant does not prove the runtime stopped or a local capture exists.

The short-lived HMAC grant has a separate protocol domain and binds that original identity, user
token ID, device, exact SSH key, issue/expiry times and a random grant ID. It expires within 15 minutes
and never outlives the issuing user token. It is kept in memory, passed in SSH stdin and excluded
from logs, command arguments, exported metadata and command journals. It is not a general bearer
credential or a Node content URL.

`POST /node/skill-state-exports/{snapshot_id}/verify` requires original Node authentication and the
forced-command device/key identity. Each verification rechecks the active user, original live user
token, device, SSH key and exact original snapshot metadata. Revocation, expiry, altered binding or
foreign identities fail closed. No startup lease or advertised preparation capability is needed to
read already-retained data. No new preparation, capture, stop, publication or deletion is authorized.

The Node must independently obtain a valid frozen Helper record and compare it with the authorized
original binding before any manifest or content is sent. If Server already has a termination or
incoming finalization digest/classification, the local capture must match it. Absence of that report
does not invent local durability. If no finalization directory exists, the separate stopped-work
path requires the Helper to verify the sealed original Native snapshot, launch/termination evidence,
and complete writer quiescence before reading and again before completion. It does not freeze,
upload or claim local durability. Existing corrupt/incomplete captures, live work and legacy sources
never permit fallback. Legacy v1 recovery retains the 100000-entry protocol bound. Explicitly negotiated recovery uses
bounded entry frames instead, as specified below. Both check signed 64-bit byte totals; runtime
admission quotas do not cap recovery bytes.

The gateway uses only a connection-local authorization window of the returned `recheck_seconds`
(1–10), measured from request start and capped by the current grant expiry. Object boundaries
check validity locally; renewal starts halfway through the window, and an independent expiry
watcher cancels blocked renewal/output. Failed or late renewal is terminal. Fresh remote proof
is mandatory before the public header and final success frame, including stopped-work recovery.
Every newly learned Server fact must remain consistent with the original observation. It sends a bounded exact manifest, streamed distinct digest objects and a final completion
frame. The CLI stages the existing portable manifest/object bundle privately, verifies every size,
digest/classification and final frame/process success, then atomically publishes an absent/empty
destination. Disconnect, revocation, corrupt/missing data and unavailable Node leave no purported
complete export. Neither a successful export nor a grant permits local content reclamation.

This contract is additive to Server checkpoint export and does not enable runtime capability
advertisement. No new database schema or content quota category is introduced. Cross-repository
implementation and acceptance must cover this entire contract before the user workflow is complete.

Request validation errors use a bounded skill error without echoing any input fields, including
malformed or oversized grants. The dedicated export routes sanitize FastAPI validation diagnostics.

## Real SSH acceptance

`AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_TEST=1 uv run pytest -q tests/test_skill_ssh_export_live.py`
uses the current host CLI, an isolated SSH agent/key, the real SSH server/forced-command gateway,
and the shipped Worker/Helper units in a disposable Linux systemd container. Identity/account/key
fixtures are explicit; takeover, deployment, session startup, termination, frozen capture and SSH
key synchronization must use production handlers. A synthetic mounted tool creates text, binary,
root-level state, permissions, an empty directory and a relative link. The test refuses finalization
uploads, restarts both daemons, then verifies every exported manifest entry/object against the
original retained capture. Mid-stream key revocation must prevent bundle publication. Export cannot
change the original work/finalization inventory or create a Server finalization.

A second case sets the real Node directory/checkpoint quota to 1 KiB before preparation. The mounted
tool generates more than 8 KiB, so no finalization directory may be created. The same real CLI/SSH
path must export the complete stopped tree without allocating a frozen copy. Runtime quota failure
cannot silently remove the large file or turn the read observation into a persisted checkpoint.

The private key stays on the host in a temporary agent; only its public registration enters the
fixture. No model credentials or inference are involved. This acceptance does not prove real model
learning, Docker Sandbox, oversized export or previous-boot writer recovery.

## Explicit continuation of a live export grant

The additive Node-only `POST /node/skill-state-exports/{snapshot_id}/renew` route accepts the same
exact forced-command identity and still-valid signed grant as `verify`. It repeats all active-user,
original-user-token, device/key and immutable snapshot checks. It issues a successor grant for at
most fifteen minutes, capped by the original user token's current expiry. Original `grant_id`, token
ID, device/key and complete binding remain unchanged. The old grant is not extended or resurrected;
it must still be valid at successor issuance, including after database observation. Revoked, expired,
foreign or changed authority can never renew. CLI login refresh cannot substitute a new user token.

The response is an ordinary uncommitted `authorized` result containing `permission`, the successor
`grant`, and `previous_grant_digest` (SHA-256 of the exact request grant). Permission reflects current
Server capture facts and the successor expiry. Consumers must retain all learned facts, exact input
and current authorization-window deadlines; receipt of another grant does not reset those checks.
Credentials remain memory-only and are excluded from logs, journals and exported metadata. Renewal
creates no database rows, content reservations or SSH-key tasks. The existing `verify` response and
fixed-expiry behavior are unchanged for older clients.

The gateway now consumes continuation while enforcing its independent short authorization window.
Helper and CLI allow transfers beyond fifteen minutes with separate fifteen-minute scan phases and
thirty-second progress bounds. Frozen readers rotate between objects after ten minutes while the
outer immutable hold remains. Actual long-transfer SSH acceptance passed in both frozen and
stopped-work cases; evidence and limits are recorded below.

The opt-in `AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_LONG=1` adds a 32 MiB file and a bounded localhost
TCP proxy in the test harness. It preserves real SSH encryption, current CLI/Node/Helper binaries
and real HTTP authorization. The first download is paced at at most 16 KiB/s for 930 seconds after
its handshake, then released; the second revocation attempt is unpaced. Acceptance requires a
successful renewal more than 900 seconds after initial verification, complete independent byte/hash
verification, failed revoked publication and unchanged source inventory. Both frozen and runtime-quota
cases use this check. This is a synthetic transport proof, not model inference or Docker Sandbox.
Updated gateways require `/renew`; deploy the matching Server before Node. A missing endpoint is
terminal and cannot become an uncertain verification replay.

On 2026-09-26 both explicit long cases passed: frozen **60917** in **955.36 s** overall and
stopped-work **30245** in **959.44 s** overall. Actual exports lasted **932.563 / 932.732 s**,
with successful renewals **930.953 / 931.214 s** after initial verification. Each source contained
9 entries / 6 distinct files / 33,562,777 bytes. Both then rejected the second export at the intended
third authority check (190, HTTP 409), removed incomplete staging and preserved the full source
inventory. The first large object is followed by five more objects, exercising frozen-reader
replacement after its original fifteen-minute connection expires. Runner-owned resources were
cleaned. These runs preceded only the later CLI partial-prefix timeout tightening and Helper request
write bound; current full gates and focused Linux/timing tests cover those changes separately.
These are legacy v1 results. The negotiated recovery format below has separate over-entry and
long-transfer acceptance; implementation alone does not imply those tests passed.


## Negotiated complete recovery stream

The fixed SSH forced command remains protocol 1. New CLI stdin adds `recovery_version:1` to the
exact `{version:1,grant:...}` handshake. Only integer 1 is supported; duplicates, nulls and unknown
fields fail. Legacy requests retain the existing complete-manifest stream. Negotiated frozen reads
also retain it. Only an absent capture, exact original retained source and independent stopped-writer
proof permit `stream_stopped_skill_recovery`. An existing Server incoming digest forbids recovery;
a later learned capture digest must match or terminate the live authorization chain. No fallback
retries an uncertain grant request or revives expired authority.

Recovery magic is eight bytes `ARSKRC\x00\x01`. Frames have a four-byte unsigned big-endian JSON
length. Header (maximum 4096 bytes) has exactly `version`, `binding`, `recovery_digest`, `unclean`,
`entries`, `file_bytes`, `file_objects`. Counts are nonnegative signed-64-bit bounded; file_objects
counts file entries including identical content at different paths. The header digest uses the
recovery domain and complete entry fields in filesystem enumeration order, never manifest v1 identity.
Private source inode digests are not sent. Complete manifest v1 stays capped at 100,000 entries.

Exactly `entries` entry frames follow, each at most 64 KiB with all eight ordinary entry fields.
Non-files have normal complete metadata. A file start has declared size, empty sha256/content_kind;
it is followed immediately by exactly size bytes and a maximum-4096-byte content frame containing
only `sha256` and `content_kind`. Source hashes during this same read; relay and CLI independently
verify all bytes before accepting trailing claims. Explicit counts bound remaining bytes/objects.
The final frame has exactly `version`, `recovery_digest`, `entries`, `file_bytes`, `file_objects`,
`complete:true`, and must repeat the original header facts after full source and writer verification.
No malformed/duplicate/unknown/null fields or trailing bytes are accepted. Initial/final scan and
ordinary progress budgets, live original authority and revocation behavior remain unchanged.

CLI privately stages format `agent-remote-skill-node-recovery-v1`, original binding, recovery_digest,
termination classification and counts in checkpoint.json; ordered complete entries in recovery.jsonl;
path-SHA256-addressed metadata in entries/; and content-addressed objects/. Its index checks unique
paths, explicit directory parents, depth, internal link targets/cycles and non-directory traversal
without tree-sized in-memory metadata. Files and metadata are verified/fsynced; whole journal digest,
footer, EOF and SSH exit success precede atomic destination publication. JSON command output retains
its common tree_digest field, interpreted under its explicit format. No v1 manifest.json is created.
This is a distinct recovery bundle, not an importable published checkpoint or retention acknowledgement.


Negotiated recovery acceptance uses the existing real SSH test with explicit opt-ins:
`AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_TEST=1` plus
`AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_CAPACITY=1` and
`AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_OVER_ENTRIES=1`, selecting `-k runtime-quota`.
The 100,001-entry run passed complete independent bundle verification, revoked retry and unchanged
source inventory (88590). A separate `AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_LONG=1` run passed renewal
beyond the original 900-second grant for this format (49223); legacy-format evidence is distinct.

`AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_DESTINATION_FULL=1` instead selects a macOS-only destination
exhaustion scenario, also with `-k runtime-quota`. It owns an 8 MiB HFS disk image and a >32 MiB
stopped-work source, observes actual pending-file growth and remaining space, requires failure
without output/staging, then retries to the ordinary destination and verifies complete content,
revocation and unchanged original source. It always detaches its own image and does not resize,
fill or prune any existing filesystem. This opt-in is incompatible with capacity/long-link modes.
