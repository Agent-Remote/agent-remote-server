# Complete-tree file authorization

Per-file downloads use the existing `skill_tree_object_references` membership and registered
`skill_content_objects` metadata. Upload completion already creates the complete tree, all unique
object references and verified object metadata in one user-locked transaction. References have
owner/category/tree and owner/category/object foreign keys. GC removes all references and their
parent tree in one transaction under the same user lock; it never publishes a partially retired tree.
The canonical manifest remains the complete directory/path/mode/link representation returned by
manifest endpoints. A file response still streams and verifies its exact SHA-256, size and content
classification from the registered immutable object; knowing a digest alone grants no access.

The content service holds the existing owner lock, requires the exact owned tree and membership,
and checks unavailable shared objects across the entire referenced tree and both quota categories.
An unrelated unavailable object does not block the tree. Another unavailable member blocks even a
healthy requested file. Node preparation and deployment retain their exact snapshot/task/attempt,
user/account, session status and lease checks before disk work and their existing reauthorization
before response. There is no authorization cache, lease extension, new public endpoint or protocol.

Migration 0055 adds only the partial `skill_object_unavailable_idx` index on `(user_id,digest)` for
objects whose status differs from `available`. The full-tree availability query starts with these
unavailable objects and probes exact tree membership. It does not load a large manifest, enumerate
all object digests into Python, or perform a full reference-count scan for each requested file.
The index is an access path, not a retention root, new state machine or independent authority.
Downgrade removes only that index. SQLite metadata creation installs the equivalent partial index.

Directory manifests still use their existing full validation path. Per-file paths preserve the
same normal transaction/GC visibility and byte checks while avoiding repeated full JSON decoding.
Tests must cover whole-tree and cross-category deletion barriers, unrelated trees, owner/category
isolation, wrong digests, exact Node binding and lease revocation, disk corruption, and the absence
of full-manifest SELECTs in file requests. Default-scale download acceptance remains separate from
upload capacity and must complete all requests before it can be claimed.

## Verification

```sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 tests/postgres_skill_upload_test.sh
AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 AGENT_REMOTE_SKILL_UPLOAD_TEST_MODE=downloads tests/postgres_skill_upload_test.sh
```

The regression mode includes content/index/migration, cross-category admission, GC/compaction,
Node snapshot/deployment binding and lease tests. On 2026-09-26 it passed all 172 selected PostgreSQL
cases (`/tmp/skill-tree-download-postgres.log`); focused host tests passed 110 cases with one
PostgreSQL-only skip (`/tmp/skill-tree-download-regression.log`). Format/lint, mypy and docstring
checks pass. These do not replace the pending full Server gate or full-scale download result.

The download capacity case writes 100,000 different ordinary files through the verified private
store after real quota reservation, completes the entire content tree, seeds that tree as an
account-directory fixture, then uses the real snapshot reservation service. Head seeding represents
prior saved content; it is not an account-publication acceptance test. The fixture simulates initial
task claiming and thereafter renews the same attempt through the actual 30-second lease endpoint.
All 100,000 file responses must pass size/SHA-256/ETag verification, with no full tree-manifest SQL
SELECT during the loop. Revoking the original task must deny a further file request. No quota or
production lease is extended for the test. Its three-hour test ceiling is not a production deadline.
The runner owns and cleans its PostgreSQL container and content root; only PostgreSQL has resource
limits (2 CPUs/1 GiB), while the client, Uvicorn and filesystem run on the host. This remains a Server
HTTP test, not proof of Worker materialization, launch, learning, publication or SSH export.

## Completed default-entry observation

`/tmp/skill-tree-download-capacity.log`: **1 passed in 2744.16 seconds**. All 100,000 unique
file requests completed in 2494.944 seconds with byte/hash/ETag verification, zero whole-manifest
SELECTs and 248 real exact-attempt lease renewals. The final revoked task could no longer read.
The runner exited 0 and removed its owned database/container/temp root. The full Server gate
also passed: 1788 tests, 95 skips, 83.03% coverage. Fixture scope described above still applies.
