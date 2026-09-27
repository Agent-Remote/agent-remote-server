# Effective account and original session queries

Existing authenticated `GET /skills` adds optional `include_system`; effective account queries always
include system references and known local sources. Ordinary lists only include system catalog rows
when requested. Catalog rows describe release selection, not installation/model-loading evidence.
Account details add exact selected branch/head/epoch, directory mode/head, expired or missing branch
state, publication/migration conflict counts and latest conflict IDs, and recorded synchronization
evidence. No query calls preparation, takeover or snapshot reservation. Disabled selections retain
honest branch information; old epochs and revisions are never substituted for current selection.

`GET /skills/sessions/{session_id}?limit=100&cursor=ENTRY_NAME` returns the user's original snapshot,
its fixed generation/directory epoch/backend/system references and bounded original member metadata.
Ordering and cursor comparison explicitly use byte collation (PostgreSQL C / SQLite BINARY),
independent of database language defaults, so clients can validate continuous name order.
Each member uses saved entry_name, state_epoch, checkpoint and resolution plus immutable branch
source/revision/installation identities. It never resolves current enabled rules, current heads or
current system releases. Retired/deleted-session snapshots remain queryable by their original owner;
content retention and snapshot lifecycle are explicitly separate from saved selection. A live legacy
session without a snapshot returns `legacy_unrecorded` with no inferred members/system versions.
Missing/foreign sessions return SESSION_NOT_FOUND. Cursors must name a member of that exact snapshot.

CLI adds `list --session ID --effective` with session-only --limit/--cursor pagination and displays
next_cursor explicitly; --session conflicts with --account-id and --tool. --include-system adds
catalog entries to ordinary lists; effective account/session queries include system entries inherently.
Account info renders optional state diagnostics. Older ordinary list/detail responses stay compatible;
new session queries require the additive Server endpoint. All output remains metadata-only, with the
ordinary 1 MiB per-response bound and no journal or runtime deployment authority.

Migration 0046 adds nullable `skill_finalizations.persisted_at` and a check forbidding a timestamp on
upload_pending rows. Complete finalization sets it exactly once in the same transaction as verified
content and the incoming checkpoint. Replay, publication, retirement and queries preserve it. Existing
rows are not backfilled from mutable updated_at or creation time. Downgrade refuses recorded times
before altering schema. PostgreSQL downgrade takes an exclusive table lock before checking,
so a concurrent finalization cannot commit new evidence between the check and column removal. Account sync diagnostics distinguish the latest recorded time from historical
persisted records whose synchronization time is unknown; they never claim complete timestamp history.
