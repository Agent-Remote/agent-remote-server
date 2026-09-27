# Native default-capacity pipeline acceptance

Run the explicitly expensive, disposable acceptance from the Server checkout:

```sh
AGENT_REMOTE_RUN_SKILL_PIPELINE_CAPACITY=1 bash tests/postgres_skill_pipeline_test.sh
```

The test creates an isolated PostgreSQL database, actual HTTP Server and systemd container with
shipped non-root Worker and privileged Helper. It installs a small valid learning skill through
the ordinary installation API. A deterministic tool inside the actual managed Native session
then creates 99,998 unique ordinary auxiliary files beside the original skill directory and
SKILL.md: exactly 100,000 manifest entries and 99,999 unique file objects. Default storage limits
and copy policies are unchanged. No sparse or duplicated files substitute for the entry count.

The original runtime exits, and ordinary background work must capture, upload and publish its
clean input. The test restarts Worker and Helper, creates a distinct session and waits for real
snapshot downloads/materialization under the original task lease. That session verifies every
auxiliary file byte, exits and must publish the same tree under a new snapshot identity. Server
rows must independently prove both clean publications and the exact final manifest cardinality.
No runtime exit, task claim, upload, preparation or publication receipt is seeded.

Fixture-only identity/account/workspace registration and unreleased Native capability admission
remain explicit; production capability advertisement stays disabled. The user token lasts eight
hours in this isolated database. Each large phase is bounded by three hours, the entire Node
case by six hours, and the outer process owner allows another two minutes for cleanup. The
PostgreSQL container is limited to two CPUs/1 GiB; Node and host Server have no explicit resource
cap. This proves neither combined 10 GiB/100,000-entry capacity, real model learning, full SSH
export, nor local reclamation. It does not substitute for the independent quota boundary tests.

The runner removes only its own database/container/temp roots. Fixed phase timings can be printed;
private connection fixtures are mode 0600 and removed on completion. Tool stdout and credentials
are not emitted. Default test collection skips this case.

The first run failed in 18.01 seconds because the test tool referenced its host artifact path
instead of Native's `/opt/agent-remote/runtime` mount. The fixture path is corrected; this was not
a production failure or capacity pass. Two further fixture runs failed before upload because the raw filesystem cardinality included
the reserved system mount root. The tool now excludes exactly the two production-reserved
system paths; the final user manifest must still have exactly 100,000 entries. The next attempt reached a clean capture and actual upload, then was deliberately stopped
to align the capacity fixture's API deadline with production: ten minutes instead of the smoke
fixture's twenty seconds. Its resources were cleaned. Current log:
`/tmp/skill-default-pipeline-capacity-bounded.log`.

The complete bounded pipeline passed on 2026-09-26: **1 passed in 11233.68 seconds** (3h 07m 13s).
First clean capture/upload/publication took 4698.5 seconds; independent successor materialization
took 2392.5 seconds; the successor verified every inherited file and its second clean finalization
completed in 4079.3 seconds. Both publications, full manifest cardinality and distinct snapshot
identities were checked by the actual runner. Its Worker/Helper and PostgreSQL containers were
removed on completion. This is the full entry-capacity pipeline described above; its byte-capacity,
model and backend scope exclusions remain unchanged.

## Actual byte and combined pipeline workloads

The same runner now supports `AGENT_REMOTE_RUN_SKILL_PIPELINE_BYTES=1`. Its deterministic tool
writes ten valid skills with twenty distinct ordinary files and exactly 10 GiB total, each skill
within the default 1 GiB checkpoint limit. There are 30 manifest entries. Ordinary sequential writes
allocate every byte; there are no sparse, reflink, hardlink or repeated-object substitutions. The
successor independently checks each instruction document and every payload byte with a fixed buffer.

```sh
AGENT_REMOTE_RUN_SKILL_PIPELINE_CAPACITY=1 \
AGENT_REMOTE_RUN_SKILL_PIPELINE_BYTES=1 \
bash tests/postgres_skill_pipeline_test.sh
```

Adding `AGENT_REMOTE_RUN_SKILL_PIPELINE_COMBINED=1` fills exactly 100000 entries / 99990 distinct
files, still totaling 10 GiB. Auxiliary bytes are subtracted from the first skill's payload. Both Node
and Server check the exact entry/object/byte counts, both clean publications and distinct snapshots.
The byte workloads require normal fresh Server-authorized reclamation after each publication; the
first session's work and frozen objects must disappear before the successor is admitted. This bounds
peak Node storage without manual deletion or lowering the default reserve. The durable reclamation
receipt and Server's stopped session are checked. Daemon restart still precedes independent inheritance.

Byte generation/verification and reclamation use the existing three-hour capacity phase budget; the
six-hour owner deadline and production transfer/authorization limits remain unchanged. Run only one
large workload at a time. Budget Docker VM space for 10 GiB work plus 10 GiB frozen objects, filesystem
metadata, build image and the default reserve, and host space for 10 GiB of Server content. The original
entry-only result above predates these added byte cases and does not establish their acceptance.

The full byte-only Native pipeline passed on 2026-09-26: **1 passed in 380.80 s**,
`/tmp/skill-default-byte-pipeline-acceptance.log`. Exact 10 GiB / 30 entries / 20 distinct files
were cleanly captured/uploaded/published in 124.5 s and normally reclaimed in 50.3 s. After daemon
restart, a new session materialized in 58.9 s, verified every instruction/payload byte, cleanly
published in 68.4 s and reclaimed in 53.1 s. Server checked both clean publications, distinct
snapshots, identical complete trees and exact capacity totals. Original work/object roots were
removed only through normal fresh-authorized reclamation. Default quotas/reserves and ordinary
copies remained unchanged; no observation overlay or seeded transition receipt was used. Runner
cleanup removed all owned containers/images/temp roots. Combined byte/entry pipeline, actual model
inference and Docker Sandbox remain separate acceptance work.
