# Node-local finalization reclamation authorization

`GET /api/v1/node/skill-finalizations/{finalization_id}/reclamation-authorization?request_id=<UUID>`
is a separate current-content check for the original authenticated Node. It does not delete content
or reuse a historic `persisted` response as permission to release the Node's only remaining bytes.
The caller supplies a new random request UUID for every read; the response echoes that challenge and
uses `Cache-Control: no-store` so an old response cannot grant a new local operation.

The endpoint shares the ordinary finalization feature gate, active-owner check, original Node and
snapshot authorization, and stopped-session check. It acquires the same user storage lock as content
GC and reference mutations, then requires:

- A terminal `published`, `conflicted` or `detached` finalization with unretired content and its
  original incoming checkpoint, tree digest and clean/unclean classification.
- The exact owner/account directory checkpoint, still retained with its complete incoming tree.
- A current terminal publication consistent with the finalization and with unretired content.
- The full manifest matching its original digest, all state object metadata present and available,
  no cross-category deletion marker, and every physical file matching its full length, SHA-256 and
  content classification through the existing no-follow private store.

Complete physical verification runs in the existing storage I/O thread. Cancellation waits for that
thread before the transaction releases its lock. Missing, corrupted, linked, retired or deleting
content fails with `STATE_RECLAMATION_UNAVAILABLE`. A valid historical finalization status can still
be returned by the status endpoint while this stronger check refuses local reclamation.

The `schema_version: 1` response envelope uses `status: reclaimable`, `committed: true` and
`retryable: false`. `committed` refers to the verified Server-retained content, not local deletion.
Its strict data schema contains:

| Field | Meaning |
| --- | --- |
| `version`, `request_id` | Protocol 1 and this read's random challenge |
| `node_id`, `user_id`, `account_id` | Original assigned Node and private content owner/account |
| `session_id`, `snapshot_id` | Immutable original session reference and reserved snapshot |
| `finalization_id`, `checkpoint_id` | Original finalization and incoming directory checkpoint |
| `tree_digest`, `unclean` | Original complete incoming content and termination classification |
| `publication_id`, `publication_attempt`, `publication_status` | Current terminal publication |
| `verified_at`, `expires_at` | Explicit UTC verification completion and 60-second observation window |

A `conflicted` observation proves available Server content only. Node marking refuses unresolved
conflict content until a later terminal publication resolves it, as required by design §5.2. Equal
publication ID/attempt cannot change its terminal status.

The publication may have advanced through a later terminal resolution, but the original incoming
checkpoint and bytes cannot change. Superseded or unresolved local publication authority is not
silently upgraded. An observation adds no persistent Server row or content lease, refreshes no
retention clock or upload expiry, and never retires or changes heads, references or quotas.

The Node transport validates the challenge and every original binding against a separately saved
terminal acknowledgement. It preserves 64-bit attempt identities, refuses redirects and automatic
retries, and derives a monotonic deadline from request start plus the Server-reported duration.
The whole request time is charged conservatively, including scanning. A response taking 60 seconds
or longer is unusable for a new local intent; the Node retains its content. Clock skew cannot grant
extra lifetime. The process-local deadline must never be persisted and reconstructed after restart.

## Remaining local reclamation work

The Server endpoint, Node transport and Linux local intent/completion journal primitives are
implemented. The journal rechecks unchanged captured work, protects unrecorded excluded paths and
retains explicit pending/completed inventory without exporting or recapturing deleted content.
See Node `docs/skill-node-reclamation.md` for the exact format and tests. These components do not
yet schedule local deletion. A separate Linux filesystem executor now consumes durable intent with
manifest checks, no-follow deletion and restart recovery. It still requires production runtime and
reference exclusion from its caller. The subsequent Helper operation must remain separate from upload acknowledgement and
runtime cleanup. Before its durable mark it must validate the original terminal acknowledgement,
verified runtime cleanup, passive absence of writers/mount aliases and all local references. It must
also ensure no post-capture work exists that is absent from the Server-retained tree. A current
authorization applies only to that exact frozen input, never takeover sources or deployment bundles.

Deletion must be two-phase, cancellable and recoverable from retained exact intent. Audit/snapshot
identity must survive removal so task replay cannot relaunch a completed session. Inventory and
export must distinguish reclaimed content from corruption and unfinished capture; neither may
recreate work or return an empty bundle as complete. Worker restart must resume a marked deletion
without borrowing authority for different data. Local reclamation is not considered complete until
these conditions, lifecycle integration and interruption/reference races have executable proof.

The PostgreSQL test explicitly contends for the same storage lock using an independent connection
and observes `pg_blocking_pids` while real object verification is paused, including cancellation.
It proves transaction/I/O ordering, not a physical GC deletion of currently referenced content.
