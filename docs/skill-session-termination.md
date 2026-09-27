# Exact managed session termination

`POST /api/v1/node/skill-snapshots/{snapshot_id}/termination` records the assigned Node's
observation of its already frozen Native work. The strict body contains session_id, task_id,
initial_tree_digest, directory_epoch, library_generation, incoming_digest and unclean. The authenticated
Node and original snapshot determine all owners; no host paths, file bytes, lease or replacement
launch authorization enter this route. A committed `stopped` envelope echoes exactly the body.
It proves control-plane acceptance of termination, never content persistence or publication.

Migration 0048 adds `skill_snapshot_terminations`: snapshot_id is its primary and foreign key;
incoming_digest and unclean fix the immutable frozen input, with creation/update timestamps.
Other identity fields remain on the immutable snapshot and are revalidated on every request.
The record contains no content reference, so it does not independently keep bytes alive. The original
snapshot's finalizing state protects pending data. No delete cascade or historical backfill is added;
downgrade refuses a nonempty receipt table.

Under the existing user storage/retention lock, session lock, then task lock, verify the unchanged
managed task pointer and original snapshot identity. Initial observations require a live session and
an uncancelled, unretired snapshot. Cancel still-pending/leased/running original create tasks without
inventing or replacing their startup result. Clean exit marks a nonterminal session stopped, unclean
exit interrupted; already-terminal statuses remain unchanged. Reserved/started snapshots become
finalizing. Device and browser bindings are revoked in the same transaction; relay closure/outbox
delivery follow commit. Replay revalidates identity and cannot reapply lifecycle transitions.

An existing finalization must match the observed digest and unclean flag. Conversely, all new or
renewed finalization begin requests must match a retained termination receipt when one exists.
Legacy terminal-session finalizations remain readable without manufacturing a termination receipt.
Late startup publication cannot revive a terminated session: unaccepted tasks are cancelled, and
already accepted startup receipts remain historical replay only. Other sessions and tasks on the Node
are untouched. This endpoint does not replace Helper writer proof, adopt broker grants or enable a
managed backend capability.

## Termination before capture can complete

Migration 0053 permits a termination observation without an incoming tree digest. Such a record
requires one fixed capture error: quota_exceeded, insufficient_storage, portability_error or
capture_failed. `/node/skill-snapshots/{snapshot_id}/capture-pending` accepts the same exact original
identity and unclean flag, plus that code; it accepts no manifest, host path or claimed durability.
The Node must freshly prove writer quiescence and read its original private termination record.
Session stopping, startup cancellation and binding revocation follow the existing user/session/task
lock order. Failed capture retains the original snapshot and work and authorizes no deletion.

The first exact frozen termination may upgrade the missing digest, preserving unclean classification
and clearing the capture diagnostic. A known digest never changes or disappears. Late capture-pending
replay cannot downgrade frozen state. Finalization begin must preserve the retained classification;
no partial manifest is published. Saving status reports capture_pending and capture_error separately
from process_stopped. Managed stop result may carry a null incoming_digest once original termination
was accepted; that immutable process result never asserts local_durable. Older frozen-only receipts
remain compatible. Downgrade refuses observations that the previous schema cannot represent.
