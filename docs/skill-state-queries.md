# Checkpoint history and exports

The Server provides active-user-token-only checkpoint history and export routes behind
`SKILL_MANAGER_ENABLED`. A checkpoint's stable identity authorizes only its owner. These routes do
not initialize branches, change library generation, reset/restore state or perform retention.

All paths below are relative to `/api/v1/skills/state`:

| Method and path | Contract |
| --- | --- |
| `GET /checkpoints?account_id=…&skill=…&limit=…&cursor=…` | All retained or expired checkpoint metadata for the specified stable source across revisions and installation epochs |
| `GET /checkpoints?account_id=…&scope=account-directory&limit=…&cursor=…` | Complete account-directory checkpoint history |
| `GET /checkpoints/{id}` | Object scope, source identity/revision/installation epoch, current branch/directory epochs, parent, head flag, retained digest and source session/finalization outcome |
| `GET /checkpoints/{id}/members?limit=…&cursor=…` | A directory's exact historical member/branch/revision/checkpoint references |
| `GET /checkpoints/{id}/diff?limit=…&cursor=…` | Explicit baseline identity/digest and bounded path metadata differences |
| `GET /checkpoints/{id}/tree` | Complete verifiable export manifest and selected/dependent roots |
| `GET /checkpoints/{id}/files/{digest}` | Verified bytes declared by that export unit |
| `GET /pending?account_id=…&skill=…&limit=…&cursor=…` | Original snapshots with incomplete finalization uploads; supports the same exclusive directory scope |

Lists allow 1–200 records; diffs allow 1–500 paths. Checkpoint cursors bind the same user, account and
stable source/directory scope. Pending cursors likewise retain their original scope even if the
previous upload has completed. Name/path cursors remain bounded. A name selects an active library or
account-local source; collisions require the stable skill UUID. UUIDs also address archived sources.
Another account cannot select a local source even under the same user. `scope=account-directory` and
`skill` are mutually exclusive. Merely reading a new user's empty account creates no library, usage
or branch records.

## Meaning of history and diff

`is_head` means this exact checkpoint is still the head of its own branch or account directory; an
old revision branch can retain its head without being the currently selected revision. The response
labels `current_state_epoch` and `current_directory_epoch` as current values, not historical values
captured by the checkpoint. The branch's source ID, installation epoch and original revision remain
explicit. `finalization_status` belongs to the source session's independent finalization. It never
means that an incoming conflict tree itself became the current head. The retained tree digest,
parent, source session and finalization ID provide the recovery references.

An item diff compares the requested checkpoint to its precise branch's original package revision or
account-local initial revision. It does not compare against today's default version. Directory diff
uses that checkpoint's parent; a root initial directory compares to itself. An unavailable baseline
returns `REVISION_EXPIRED` or `STATE_EXPIRED`, never an invented empty baseline. Paths preserve their
original account discovery-root prefixes, including item root permission changes. The response
contains entry metadata, digests and sizes; it does not load binary or large-file text.

Complete deletion of a known item retains a separate empty item view during finalization, including
unclean/detached input. The complete directory does not falsely claim a surviving member. This makes
the item deletion discoverable and explicitly exportable as `locally_removed=true` without confusing
it with missing uploaded bytes. Older input directories remain retained; the new view behavior needs
no schema change.

## Export layout and integrity

A directory export returns its entire stored manifest. An item export returns the complete connected
link unit containing its original subtree: paths and link targets remain unchanged, and
`dependency_roots` lists all additional top-level roots. Independent members are excluded. The caller
must preserve this manifest layout; stripping the selected prefix can make cross-entry links escape
the destination. A future CLI exporter must stage the verified complete unit in an empty destination
and publish it only after every file and link has been safely materialized.

`source_tree_digest` identifies the original complete stored tree. `tree_digest` identifies the exact
export manifest, which can differ when independent members were excluded. File requests require
membership in this export manifest; knowing a digest elsewhere in the larger backing tree does not
authorize this endpoint to return it. Content is copied into a private bounded-memory spool with
size/digest verification, then the user lock and request transaction are released before streaming.
Missing files return `CONTENT_INCOMPLETE`; corrupt bytes return `CONTENT_INVALID` before any successful
file response. This verification also applies to the shared package and conflict content reader.

Expired metadata stays inspectable with `storage_location=expired`; export/diff fail explicitly.
Pending uploads identify the source Node, original snapshot, original session and incoming digest,
with `exportable_from_server=false`. They have no exportable checkpoint and cannot produce a claimed
complete empty directory. Online Node export of pending local content remains integration work.

Current-effective selection/default state diff and atomic reset/restore are implemented separately
in `docs/skill-state-mutations.md`. Retention/GC, local Node export and CLI destination materialization
remain unfinished. Managed public admission
and runtime capability advertisement remain gated pending the other cross-repository work.

Read-only conflict listing also uses the existing-library read lock, so querying an empty account's
conflicts cannot create usage or library rows. Content upload and resolution commands keep their
separate mutation lock/commit boundaries.

## Historical provenance (0037)

Checkpoint list/detail now reports `state_epoch` (item creation epoch), `directory_epoch` (directory
creation epoch) and `backing_directory_id` (the item's exact full-tree context), separately from
`current_state_epoch` and `current_directory_epoch`. Legacy unknown values remain null; an independent
original-package item legitimately has no backing directory. Directory member pages also report the
member checkpoint's historical `state_epoch`, never the branch's current value. Content retirement
preserves these audit fields. See `skill-checkpoint-provenance.md` for the ownership/content FK and
creation rules; this metadata does not by itself authorize publication.
