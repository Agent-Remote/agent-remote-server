# Skill content storage

This implements the private content foundation of the approved
[skill manager design](../../agent-remote/docs/skill-manager-design.zh-CN.md).
It now backs the authenticated package HTTP API and library services, behind
`SKILL_MANAGER_ENABLED=false` by default. Account checkpoints, Node authorization, CLI commands
and retained-history GC still need to be connected before the complete manager can be released.

`SkillContentService` accepts the authenticated user identity from its caller. Its repository
owns every SQL query; the service does not commit the caller's transaction. Mutations acquire the user's storage write lock. Read paths lock an existing user row without
creating a library, incrementing counters or changing persistent state.
Disk operations run in a worker thread and cancellation waits for that thread to finish before
releasing its streams or the database lock.

The caller must commit `begin` before uploading files. The persisted plan binds its idempotency
key to the exact manifest digest and scope. Reuse with a different payload fails. Missing unique
file bytes are reserved atomically against retained content plus other outstanding reservations.
Package staging is additionally limited independently. Runtime uploads use the runtime allowance;
they are not limited by the installation package's 10 MiB file limit. The current implementation
serializes disk work for a user through the transaction lock as a correctness baseline.

Each file is streamed through length, SHA-256 and incremental UTF-8 validation. Objects are
isolated by user even when their digests match. The writer fsyncs the file and directories before
publishing an immutable object. Repeated uploads validate the existing object instead of replacing
it. Descriptor-relative directory access and no-follow opens reject replaced symlinks and special
files. The deployment must provide a trusted parent directory; the content root and descendants
must be service-owned mode 0700. Stored objects are mode 0400.

`complete` rechecks every declared file before atomically registering the complete tree, its
explicit foreign-key-protected object references, and actual usage. It releases the reservation
in the same transaction. A crash after disk publication but before database commit leaves the
original upload retryable. Uncommitted bytes cannot be downloaded through the service: downloads
must name an already committed tree owned by the caller. Complete is idempotent and does not mean
that a library revision or account checkpoint has been published.

Expired plans cannot accept or publish files. Their reservations are released exactly once when
another begin/status call visits that user. Before releasing a reservation, the collector removes uncommitted bytes with no live upload or
registered reference in either quota category. It also removes orphan blobs and interrupted
upload files older than the staging retention period. Failure rolls back quota release; retry
is idempotent. Committed trees and revisions are not removed by this staging collector. A future
retained-history worker must use the same user lock and graph of tree references; it must
never treat Node-only state as upload cache. Migration downgrade removes metadata only and must
follow an explicit content export; it deliberately does not delete files.

Configuration uses `SKILL_STORAGE_ROOT` and a JSON object in `SKILL_STORAGE_POLICY`. All policy
sizes are bytes; defaults match design section 8.2. For example,
`SKILL_STORAGE_POLICY={"user_package_bytes":4294967296}` overrides just the retained package quota.
The database and content volume must be backed up together. No feature capability is enabled by
these settings alone.

Validation:

- `uv run pytest tests/test_skill_storage.py tests/test_skill_content_service.py`
- Set `SKILL_TEST_DATABASE_URL` to an isolated PostgreSQL database to run the same service tests
  against production database locking and foreign keys.
- Alembic `0026_skill_content_storage` upgrade/downgrade/upgrade was exercised on PostgreSQL 17.
  The migration adds content objects, trees, tree references, upload plans, and usage rows without
  modifying existing account or session modes.


The package HTTP API is rooted at `/api/v1/skills/content`. It accepts only current user tokens,
not device or Node credentials. `POST /uploads` reads a manifest body bounded to 64 MiB;
`PUT /uploads/{id}/files/{sha256}` streams only a declared file and checks exact size/hash/type;
`POST /uploads/{id}/complete` verifies the complete tree. Query routes return the original upload
plan, an owned tree manifest, or a file reached through that tree. Runtime uploads will require
separate exact session/snapshot or conflict authorizations. No account state binding can be
supplied to these package endpoints.

## Retained-content reclamation

The internal retained-content release and worker protocol is now defined in `skill-content-gc.md`.
After exact tree/history authorization, quota-bearing objects are those with `status=available`.
A `deleting` object remains registered only as a shared user/digest admission barrier: its category
quota has already been released in the same transaction that created its persistent deletion task.
Worker completion removes the marker without deducting usage again. Active same-category uploads,
including zero-reservation deduplication leases, keep their original object and quota until released.

Disk deletion is separately retried by original task UUID. Private `.deletions` completion receipts
and user-directory flock fence delayed I/O after database connection loss, in addition to normal
user transaction locks. The staging collector preserves these receipts and all registered markers.
The whole private volume, including completion receipts, must be backed up together with the matching
SQL state while all writers and disk threads are stopped. Public prune/receipt integration and final
cross-component acceptance remain outstanding.

## Indexed upload declarations

`docs/skill-upload-object-index.md` and migration 0054 add an atomic, owner/upload/tree/scope-bound
projection of the original unique file declarations. File requests load only current upload metadata
and the selected declaration; legacy uploads backfill under the user lock after validating their
original manifest digest. Completion still validates the full canonical input and hashes every byte.
Terminal transitions remove projection rows without changing original audit records or quotas.

Retention graphs and GC forecasts share the same active-upload interpretation. Indexed projections
carry a checked unique-object count and consume the existing row budget; missing references make the
whole analysis fail. Only legacy staged inputs need full manifest JSON in retention analysis. This
keeps the 16 MiB metadata guard while admitting a standard 100,000-file upload whose full JSON exceeds
that guard. A zero-reservation upload still protects its original objects in both quota categories.

Run real PostgreSQL regressions and the explicit default-sized network test separately:

```sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 tests/postgres_skill_upload_test.sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 AGENT_REMOTE_SKILL_UPLOAD_TEST_MODE=capacity tests/postgres_skill_upload_test.sh
```

The runner owns an isolated loopback PostgreSQL container, applies all migrations, then removes only
its container and temporary test root. The capacity case uses a real Uvicorn listener and authenticated
Node routes for 100,000 distinct ordinary objects with unchanged storage policies. It counts full
upload-manifest SQL reads during the object loop, completes through full byte verification, and checks
that declaration rows retire only after content persistence. The three-hour fixture ceiling allows
serial network/transaction/filesystem work; it does not change production operation deadlines. The
DB container has 2 CPUs/1 GiB; Server, client and content files run on the host without an imposed
memory/CPU cap. This is HTTP/storage capacity, not managed-runtime or full Worker lifecycle proof.

## Default byte-capacity acceptance

Two additional opt-in modes keep the production policies unchanged:

```sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 AGENT_REMOTE_SKILL_UPLOAD_TEST_MODE=bytes tests/postgres_skill_upload_test.sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 AGENT_REMOTE_SKILL_UPLOAD_TEST_MODE=packages tests/postgres_skill_upload_test.sh
```

`bytes` requires at least 44 GiB free on the temporary filesystem. It writes full random ordinary
source files, checks their allocated blocks, then sends every file through real authenticated Node
HTTP routes into the private object store. Ten valid skill directories of exactly 1 GiB make the
first 10 GiB complete directory; a second independent snapshot fills the remaining runtime allowance,
including the fixture's existing retained baseline, to exactly 20 GiB. Both uploads reserve capacity
before transfer. Assertions cover one-byte excesses for the item, complete directory and user quotas,
actual SQL object-byte totals, transitions from reservations to stored bytes, full completion and
idempotent completion replay, physical object sizes, and terminal declaration-index removal.

`packages` requires at least 5 GiB free. A separate empty user reserves 41 complete package manifests
at once, then transfers all 205 unique files through real user authentication and the public package
routes. Individual files stay within 10 MiB and packages within 50 MiB. The test fills both initial
reservations and final retained package storage to exactly 2 GiB, rejects another byte, checks the
per-file and per-package limits, and verifies runtime accounting stays zero. At unchanged defaults,
the 2 GiB retained-package bound coincides with the staging bound; this proves their combined default
admission behavior, not an independently isolated staging-only policy branch.

Both modes use the same isolated PostgreSQL runner and cleanup as the object-count acceptance.
Sources are fully written and hashed, without sparse files, virtual readers, repeated-content quota
shortcuts or reduced limits. Uvicorn/client/storage run on the host; only PostgreSQL is capped at
2 CPUs/1 GiB. Runtime exit observations are simulated after real snapshot reservation, so these tests
prove Server network/storage quotas rather than actual Helper capture, Worker transfer, runtime
launch, account publication, export or production throughput. The object-count and byte tests are
separate cases and do not claim a single simultaneous 100,000-entry/10 GiB pipeline.

Recorded on macOS ARM64 with concurrent unrelated acceptance tests (2026-09-26):

| Case | Full result | Evidence |
| --- | --- | --- |
| 10 GiB directory, 1 GiB items, 20 GiB retained runtime total | Passed in 294.72 s; runner exit 0 and cleanup complete | `/tmp/skill-upload-byte-capacity-isolated-limits.log` |
| 2 GiB package reservations and retained package total | Passed in 49.46 s; runner exit 0 and cleanup complete | `/tmp/skill-package-byte-capacity.log` |

The final runtime fixture fully wrote its random input files in 174.415 s; its first directory
completion was at 230.948 s and both complete directories at 291.230 s, measured from fixture generation start.
These are observations for this host, not production latency or memory guarantees. The earlier
runtime pytest also passed, but its shell wrapper encountered a concurrent script-edit parse error;
a fresh run passed with runner exit 0 in 304.07 s. Review then isolated the directory-only rejection
from the independent item limit by adding one root auxiliary byte to an otherwise valid 10 GiB
directory. The final log above repeats the entire successful runner with that stronger assertion.

## Tree-member downloads

`docs/skill-tree-downloads.md` and migration 0055 cover file requests through the existing normalized
complete-tree/object references. These requests retain the owner lock, full-tree cross-category
deletion barrier and byte verification while omitting full manifest JSON. Node preparation and
deployment keep their original task and lease checks before and after copying. Manifest endpoints
continue returning the complete canonical directory representation. The opt-in `downloads` runner
mode must finish all 100,000 authenticated requests before default-count download acceptance is met.
