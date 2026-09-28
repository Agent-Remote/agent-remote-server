# Server Skill manager

This is the Server-specific reference. Read the shared
[operator guide](https://github.com/Agent-Remote/agent-remote/blob/main/docs/skill-manager.md),
[mandatory contract](https://github.com/Agent-Remote/agent-remote/blob/main/docs/skill-manager-wire-v1.md)
and [acceptance status](https://github.com/Agent-Remote/agent-remote/blob/main/docs/skill-acceptance-plan.md).
Keep current behavior here and exact fields in schemas/tests, rather than adding per-stage documents.

## Configuration and storage

`SKILL_MANAGER_ENABLED=true`, `SKILL_STORAGE_POLICY={}`, `SKILL_DELETION_INTERVAL_SECONDS=30` and
`SKILL_DELETION_BATCH_SIZE=100` are the installation defaults. Explicit false disables new gated routes/admission;
it never authorizes legacy writers into an already managed account or discards pending data.

Mount persistent `/var/lib/agent-remote/skill-storage` with its private `content` child. Missing storage yields
503 `CONTENT_STORAGE_UNAVAILABLE`; do not create an ephemeral parent to hide a missing volume. Update the deployed
Compose file as well as the image. Backup database metadata and content together; Node-retained pending bytes need
coordinated Node backup. Quota and retention fields are defined in
[storage/policy.py](../src/agent_remote_server/skill_manager/storage/policy.py).
The deletion worker consumes committed tasks; its interval does not retire all historical content.

## Service and transaction boundaries

- User-token-only library/state APIs derive owner identity. Node preparation, takeover, deployment and finalization
  use exact authenticated original bindings; knowing a digest never authorizes reading another account's content.
- Pure manifests/rules/merge/storage live in `skill_manager`; services orchestrate repositories, and routes use typed
  schemas. ORM models and Alembic migrations evolve together. Content bytes stay out of ordinary results/audits.
- Content completion, library acceptance, deployment, runtime readiness, finalization persistence and publication are
  separate commits/receipts. A stored unbound target deploys on first use; it is not proof of model loading.
- Store immutable configuration plans and exact target attempts. Global/tool mutations select changed effective
  enabled sources; explicit account requests and unfinished attempts remain meaningful. Poll-time scheduling is bounded
  and precedes task locks, one owner transaction at a time. Dispatch rechecks fresh capabilities and exact leases.
- Initial takeover serializes with imports and writer admission under the user storage lock. Server task terminality
  cannot prove Node process quiescence. Managed accounts and migration profiles fence legacy writer paths independently
  of the feature flag; managed snapshots are excluded from generic runtime-list reconciliation.
- Slow content transfer releases preliminary database locks, then reauthorizes before publishing bytes/references.
  Bound upload membership queries and deletion barriers to the exact tree; retain normalized object indexes for capacity.
- Finalization first records original stopped evidence, then complete durable content, then a separate publication.
  Publication atomically validates all affected heads/epochs and the complete account directory. Unclean/old-epoch
  input remains detached; conflicts preserve original comparison trees. Pending data blocks session/account deletion.
- State preview and diagnostics remain read-only. reset/restore/migrate/resolve/prune use exact confirmed input,
  immutable operation receipts and original request keys. Changed heads/epochs cannot silently rebase confirmation.
- Retention updates references and last-release clocks in the same user transaction. Protect active/pinned/disabled,
  pending/conflicted, upload and cross-category references. Unknown graph state fails closed. Prune discloses the complete
  effect; two-phase GC commits deleting/quota changes before physical removal and retries without losing audit evidence.
- Node reclamation authorization rechecks current complete physical content and terminal original publication under
  the GC lock; its 60-second observation is not a perpetual grant. Export renewal preserves original user/token/device/key
  and snapshot, cannot revive expired authorization and never creates a checkpoint or changes heads.
- Backend migration recovery keeps the original failed task/result and exact source/target binding. v1 verifies target,
  v2 verifies source restoration, v3 explicitly repairs source permissions. Request/action is immutable; repeat current
  admission checks under the same user/account lock and never reactivate an administratively disabled account.

## Implementation and verification

| Path | Responsibility |
| --- | --- |
| [skill_manager](../src/agent_remote_server/skill_manager) | Pure manifest, merge, dependency, storage and retention algorithms |
| [services/skills](../src/agent_remote_server/services/skills) | Library, runtime, deployments, state operations and retention orchestration |
| [schemas](../src/agent_remote_server/schemas) / [api](../src/agent_remote_server/api) | Exact versioned request/response and authorization contracts |
| [repositories](../src/agent_remote_server/repositories) / [migrations](../migrations) | SQL access, reference graph, indexes and schema history |
| [tests](../tests) | Skill contract, storage, migration, concurrency, lifecycle and recovery tests |

Run `scripts/run-quality-checks.sh` for the required formatter, lint, typing, test/coverage and docstring gates.
PostgreSQL locking tests and real production/model scenarios require separate environments; skips are not passes.
The shared acceptance document is the sole current record of completed evidence and remaining real-world validation.
