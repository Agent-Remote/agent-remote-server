# 02 Architecture

Skill library target selection compares enabled sources before and after global/tool mutations,
while preserving explicit account requests and unfinished attempts for affected sources. See
`../skill-deployment-plans.md`; disabling/removing an enabled source still requires deployment.
Content upload filesystem errors return HTTP 503 `CONTENT_STORAGE_UNAVAILABLE` with a sanitized
volume/permission hint, preserving the original upload lease for recovery after storage repair.

Backend account migration results are serialized with account writer admission under the user
content lock. Only the original task/user/account/node/source/target binding may publish a result;
exact terminal result replay cannot mutate a newer migration. Conflicting or malformed completion
is rejected before task/result/account changes. Failure preserves the source backend and records
recovery_required without reactivating the account. Generic failure is not source rollback proof.
Persistent migration profile state gates binding, backend replacement, session creation and config
imports independently of mutable account.status, with fresh reads under the same lock. Historical
failed profiles also require recovery. No automatic recovery or rollback authorization is implied.

Generic Node runtime-list reconciliation only mutates legacy sessions without a persisted skill
snapshot. A missing runtime in that list can be an observation made before a queued managed launch;
it cannot interrupt a managed session, revoke its connections or enqueue legacy cleanup. The saved
snapshot's stable session reference is the guard, independently of mutable task markers, current
feature flags or snapshot phase. Managed startup, termination and explicit stop keep using their
separate original-snapshot-bound evidence. This also preserves clean natural-exit classification.

Node-local finalization reclamation requires a separate current-content authorization read at
`GET /node/skill-finalizations/{id}/reclamation-authorization`. Under the same user storage lock as
GC, it rechecks the original active owner/Node/stopped session, retained incoming directory checkpoint,
terminal current publication, non-deleting state objects and every physical object's complete bytes.
Only then can it return a 60-second observation bound to the exact original identities, digest and
clean/unclean classification. Historical persisted receipts alone are insufficient. This endpoint
does not delete, retire, renew upload leases, mutate heads or authorize removal of other local data.
The Node must separately verify its original acknowledgement, runtime cleanup and absence of local
references before its own durable two-phase reclamation. See `docs/skill-node-reclamation.md`.

Frozen Node-only snapshot export follows `docs/skill-node-export.md`: live user authorization
creates a bounded grant for one original snapshot and exact device/SSH key; the original Node
reauthorizes that grant online through a separate route. SSH carries only already-frozen Helper
objects, independently of upload quota or startup leases. Export never creates runtime authority.

Deployment attempt services separate immutable configuration selection from mutable execution
progress. Status holds the existing user read lock and refreshes operation state before validating
the complete attempt chain. Retry checks the original owner, generation, exact failed attempts and
current binding/selection before appending all successors in one retention transaction. It never
fetches upstream content, replays successful targets or grants runtime execution by itself.
See `docs/skill-deployment-attempts.md`; actual Node dispatch remains a separate boundary.

User retry submission and receipt lookup use the original operation's `/retries` subresource.
POST commits the complete validated attempt set; GET only observes an existing owner-scoped retry
key and original operation. Both require the existing live user token and feature gate. Receipt
lookup cannot infer acceptance merely from a later operation state or a matching library key.

Managed Native create-task completion is guarded by the exact original snapshot and poll attempt.
Its bounded outcome carries only session/account/snapshot/task identity and neutral runtime metadata.
The existing Node task result transaction owns persistence and lifecycle revocation; the skill guard
locks/rechecks original authority and makes exact committed replay a mutation-free historical read.
It must never revive a cancelled task or reapply old readiness to a stopped session.

Custom conflict resolution supports a separate metadata-only preview before uploading file bytes.
The preview accepts one exact proposed manifest and plan revision, reuses the saved conflict inputs
and pure resolution algorithms, and never creates uploads, content grants, plans, receipts or heads.
Its response always says content_verified=false and ready_to_publish=false; a complete metadata
candidate is not a publication authorization. Actual resolve continues to require fully uploaded,
owner/conflict-authorized content and all source, quota and head checks. Preview input cannot enter
the mutation request schema or be used to bypass normal resolution validation.

Session conflict listing accepts an optional stable skill/name selector. Authorization resolves the
source within the exact user/account, and the repository filters saved publication branches before
pagination, including unchanged observed branches. Cursor membership uses that same source predicate.
Migration conflicts retain their independent list and saved-input cursor protocol; callers must not
combine both domains into a single ambiguous page or compare migration inputs against live heads.

## Module Layout

```text
src/agent_remote_server/
  api/          FastAPI route modules and dependencies
  device_control/ Device-control release, relay, retention, and limit infrastructure
  ego_browser/  Ego-browser relay and cleanup infrastructure
  middleware/   ASGI middleware
  models/       SQLAlchemy ORM models split by business domain
  port_forwarding/ Port-forward token and cleanup infrastructure
  relay/        Cross-domain relay identity primitives
  repositories/ Database access helpers
  schemas/      Pydantic response and request models
  security/     Password, token, encryption, and TOTP helpers
  services/     Application services, with large domains split into operation packages
  config.py     Environment-driven settings
  context.py    Request-local context
  db.py         SQLAlchemy engine and database helpers
  logging.py    Structured logging
  main.py       FastAPI application factory
  redis_client.py
```

## Dependency Direction

- `main.py` wires application components.
- `api/` may depend on `schemas/`, `config`, `db`, and `redis_client`.
- `services/` may depend on `repositories/`, `models/`, and `schemas/`.
- `services/` may depend on `security/` helpers for explicit security operations.
- `repositories/` may depend on `models/` and `db`.
- `security/` must not depend on API, database, repositories, services, Redis, or middleware modules.
- `models/` may depend on `db` for the declarative base.
- `schemas/` must not import API, database, Redis, or middleware modules.
- `middleware/` may depend on `context` and standard logging only.
- `db.py` must not import API route modules.
- `redis_client.py` must not import API route modules.

## User Skill Manager

Pending acceptance and bounded poll-time scheduling follow `docs/skill-deployment-scheduling.md`.
Acceptance uses explicit policy and saved compatible reports; dispatch independently requires fresh
authority. Schedule before poll task locks and commit one owner at a time. Never infer task drain
from a superseded configuration or allow the scheduler to terminate an existing task binding.

Exact managed termination uses a separate authenticated Node route and immutable snapshot receipt
as specified in `docs/skill-session-termination.md`. It coordinates session/task termination and
device/browser revocation under existing retention locks, separately from content persistence.
Partial background inventories must never masquerade as complete runtime reconciliation snapshots.

Library acceptance records each target's immutable configuration projection in the same user
transaction. Normalized revision references and a canonical plan digest prevent retry inputs
from following later rules. The plan is independent of managed session snapshots and grants no
execution capability. See `docs/skill-deployment-plans.md` for persistence and retention boundaries.

The approved cross-repository contract is `../agent-remote/docs/skill-manager-design.zh-CN.md`
(resolved from this repository root); its wire contract is `skill-manager-wire-v1.md` alongside it.
`skill_manager/` owns pure manifest/rule/merge logic and bounded private object storage;
`services/skills/`, repositories, schemas and API routes retain their existing dependency direction.
User libraries are private, tool/account overrides resolve field by field, and session snapshots
fix exact revisions. Mutable session copies never write immutable packages. Runtime content is
uploaded through authenticated private storage, never through heartbeat, task result or audit bodies.
Finalization publishes a complete account-directory change only after content is durable and all
generation, epoch and head preconditions hold. Stops and deletes preserve unuploaded state.

Skill HTTP surfaces keep content staging/completion, library mutation, rule changes and operation
queries separate. New legacy session admission rejects an account with any effectively enabled
user-library skill before creating a session or startup task; it must never silently omit installed
skills until takeover completes. Managed accounts use full-account preparation and exact snapshot
reservation after per-backend capability verification. Existing session lookup and attach retain
their original runtime. `SKILL_MANAGER_ENABLED` gates admission and the new routes during rollout;
it does not upgrade an account to managed mode. User writes retain an explicit database
commit boundary; errors roll back the request. Package streams are bounded by an authenticated
upload manifest before accepting file bytes. No generic remote-path write endpoint is introduced.

Account binding and backend-migration planning also acquire the user skill-storage lock before
changing account/profile/task state. Nonlegacy accounts reject these existing legacy-writer paths
with `MIGRATION_PENDING`, independent of the feature switch. A future managed binding/migration
adapter must provide its own verified snapshot and retention protocol before reopening them.
The Node's persistent fence rejects already queued legacy writers; these planning guards do not
establish process quiescence and never stop an existing session for takeover.

The internal account takeover transaction is specified in `docs/skill-account-takeover.md`.
Reservation and final authority publication use the same user lock as runtime/import admission,
with savepoints protecting all partial references. Server task/session terminal status is only
control-plane drain evidence; the Node must independently establish actual writer quiescence.
Ordinary Native session admission now reserves and reuses exact takeover tasks; capability
advertisement and Docker/sbx takeover remain separately gated.

Authenticated Node takeover transfer uses `/node/skill-takeovers/{id}` and the exact task UUID
on every request. Status returns only immutable identities/inventory and the original receipt;
it remains readable while old writers drain. Capture bodies are bounded to 64 MiB and authorized
before reading. File streams verify their manifest membership, length, digest and classification
before storage, then reauthorize before commit. Only complete publication acknowledges authority.
These routes do not reserve takeovers or enable automatic dispatch.

Takeover capture/file routes commit their preliminary authorization transaction before waiting for
request bytes, then reacquire authorization before mutation. This permits concurrent task renewal
and revocation without admitting bytes under a stale grant. The takeover-only lease endpoint renews
an unexpired leased/running task for its current poll attempt, under the same user/task locks;
it cannot revive expired/terminal tasks or renew a committed takeover. Poll envelopes expose the
additive `lease_attempt` counter. Existing logical task result identity remains unchanged.

Account-local configuration uses the same user lock, generation CAS and operation ledger as library
rules, with separate local-source models and views. Shared source resolution rejects ambiguous names
before mutation; local sources require an explicit owning account and never receive library override
rows. Local enabled changes leave all runtime heads, epochs and session snapshots untouched. See
`docs/skill-library-api.md` for the additive list/detail contract and supported local commands.

## Application Factory

Use `create_app(settings: Settings | None = None)` for testability. Tests should inject explicit settings instead of mutating global environment whenever practical.

## Health Checks

- `/healthz` checks process-level health and must not depend on external services.
- `/readyz` checks dependencies required to serve traffic.
- Dependency failures must return a structured degraded response instead of crashing the process.

## Runtime Backend Control

- The control plane owns backend policy; clients cannot select a backend when creating a session.
- A tool account pins `docker_sandbox` or `native` when it first binds. Sessions inherit that value.
- Backend changes use an explicit node task and commit only after node-side verification succeeds.
- Nodes report independently probed backend capabilities. Scheduling uses the intersection of the administrator allowlist and the reported capabilities.
- Inactive native resources reported during reconciliation move active sessions to `interrupted`; process exits may enqueue idempotent runtime cleanup without replacing that status, and the control plane never replays their commands.
- Node task leases cover both delivery and execution. An expired non-terminal lease is reissued so
  the node can replay its locally persisted terminal result and converge the control-plane state.
- Users may delete only `stopped`, `interrupted`, or `failed` tool sessions. Collection deletion removes all sessions in those three states for the current user; active lifecycle states remain protected and all deletion paths are audited.
- SSH forced commands use a stable device gateway. Attach and sync access are re-authorized against the control plane on every connection.
- SSH agent forwarding is authorized only for active developer credential profiles that explicitly select `agent_forwarding`; both the client attach response and the node forced-command verification carry that decision.

## CLI Remembered Login

CLI remembered login uses `services/cli_login_sessions.py` and an independent refresh
endpoint. It does not extend ordinary user-token expiry, device identity, browser bindings,
or full-trust authorization. See the identity and persistence rules for rotation and revocation.

## Device WireGuard Enrollment

- A device-scoped token may create or update only its own active WireGuard peer.
- The control plane accepts and stores only the device public key; private key generation and storage remain local to the CLI.
- Re-enrollment keeps the existing peer ID and interface address so local repair does not change routing unexpectedly.
- WireGuard public key bodies must not be written to audit details or logs.

## Local Device Control Sessions

- `device_sessions` binds one macOS device to exactly one user-owned remote Claude session and
  its assigned node. The server is the only binding source of truth; every runtime operation
  matches user, device, tool session, device session, node, and generation.
- The Device APP uses its device token to list minimal running Claude candidates and atomically
  claim one candidate for itself. The server derives user and device identity from the token;
  the request cannot override either identity.
- At most one non-terminal binding may exist for a tool session and, in the first release, for a
  device. Claiming stops conflicting bindings with reason `rebound`, preserves their audit
  history, and creates a new `pending_device` record whose authorization comes from the fresh
  local session selection.
- Only the bound device token may report the local connection, install the Server-selected
  session authorization, acquire the machine lock, renew the lease, reconnect, or invoke
  device-side stop. The owning user and an administrator may end control, but cannot replace
  local session selection or choose a stronger authorization mode.
- Reconnect and current-action abort increment the generation and clear the old lease while
  retaining an acquired machine lock. Only explicit session end, confirmed remote-session
  failure, or lease expiry releases the lock. Stop increments the generation and clears both.
- Generation is a positive signed 64-bit value. Non-terminal sessions are capped at
  `9223372036854775806`, reserving `9223372036854775807` for a final stop transition; an
  exhausted generation is rejected before state, audit, or Node-task mutation.
- The control plane stores lifecycle and authorization metadata only. New bindings explicitly
  store authorization mode, policy version, and authorized time; historical application approvals
  remain compatibility data. Application names, bundle identifiers, clipboard content, GUI content,
  input, coordinates, titles, images, certificates, and plaintext relay payloads are forbidden.
- Relay and one-time connection material are separate short-lived infrastructure concerns and
  must not be added to the business session row.
- The bound device and assigned node register one ephemeral SPKI digest per generation. Redis
  exchanges each role's opposite-peer pin, shared 256-bit exporter context, and one-time relay
  ticket exactly once; none of these values enter SQL or structured logs.
- The device relay consumes role-bound tickets atomically, pairs only opposite roles with the
  same complete session binding and generation, accepts binary frames only, and enforces explicit
  per-frame, per-direction byte-rate, peer-wait, and connection-lifetime limits. The byte-rate
  limiter permits a bounded two-second burst so encrypted image frames can arrive together;
  sustained excess still closes both peers. When either peer
  disconnects or reaches a connection limit, the relay closes both endpoints so the surviving
  side cannot retain a dead one-time generation. It never parses or persists the nested TLS byte
  stream.
- Creation requires the assigned node's latest independent capability report to explicitly
  support protocol version 1, `platform=macos`, and the tool session's pinned runtime backend.
  A missing, stale, malformed, or incompatible report is denied rather than inferred.
- Creation and every generation change enqueue an idempotent `activate_device_control` task for
  the bound node. Terminal stop enqueues `deactivate_device_control`; these task payloads contain
  only binding and runtime location metadata, never relay tickets, keys, pins, or exporter data.
- A tool session records `device_control_protocol_version` only when its original Node creation
  task included the managed MCP configuration. Device-control creation requires this persisted
  fact and never infers injection from a later heartbeat.
- `device_control_enabled` defaults to false. Deployment operators may enable it only after the
  selected release profile's fixed gates pass. `apple-developer-id` requires signing,
  notarization, outbound allowlist, compatibility, and security review. The reduced
  `community-local-trust` profile requires explicit risk acceptance, project self-signing,
  official-runner automation, manual installation trust, and application-enforced egress.
  Production startup additionally requires an Ed25519-signed release-evidence manifest bound to the
  exact root distribution and server version. Current schema 9 is permanently valid for that signed
  version/component/artifact combination; it has no time-based expiry. Already issued schema 8
  manifests retain the same permanent verification semantics, but may authorize only the legacy
  `per_application_approval` policy. Production `session_full_trust` requires schema 9 at startup
  and throughout guarded HTTP and relay operations. The manifest pins the server, Node, application, proxy, SBOM, provenance,
  security-test, review or risk-acceptance, signing, outbound-policy, local-Claude-isolation,
  stop/revocation, and compatibility evidence digests. Schema 2 additionally pins community
  signing, official-runner automation, and risk-acceptance digests. Schema 3 binds every supported
  Linux Node and proxy target in one community manifest. Development may explicitly enable the
  capability without this production-only evidence gate for non-sensitive test data.
- Computer Use v2 is enabled by default for new generations. The Server negotiates it only when the
  assigned Node advertises the complete required capability base, then includes recognized optional
  extensions. Partial, unknown, or malformed capability sets fall back atomically to v1.
  `device_control_v2_enabled=false` is the emergency switch for new generations. Optional schema 9
  quality evidence does not authorize runtime capabilities, and capabilities never change within an
  active generation.
- `session_full_trust` additionally requires the complete set `session_full_trust_v1`,
  `application_launch_v1`, `global_clipboard_v1`, `clipboard_payload_v2`, and the three v2 base
  capabilities from Node/proxy, plus the exact Device claim capability `session_full_trust_v1`.
  Capability validation follows the binding's persisted authorization mode, not a later deployment
  policy value. Legacy bindings filter out all three full-trust-only capability names; active
  same-generation lease renewal preserves its mode even if the v2 switch later closes.
  Legacy policy accepts a missing Device capability field; full-trust policy rejects missing,
  duplicate, unknown, or mixed Device capabilities with an upgrade error before creating or reusing
  a binding. Missing or mixed Node capabilities make the candidate uncontrollable instead of
  falling back to different authorization semantics.
- Production device control requires explicit non-zero retention periods for terminal device-session
  metadata and device-session audit metadata. A bounded background service deletes only terminal
  sessions older than the configured stop-time cutoff and audit rows whose target type is
  `device_session`; it never deletes active sessions or general identity audit records.
- App end, Web stop, device revoke, lease expiry, tool-session stop, and Node reconciliation all
  use the same revocation service. Ending device control leaves the remote Claude session
  running; stopping or losing the remote Claude session also revokes its live device binding.
- Database revocation closes already established relay pairs through cross-worker Redis
  notification. A local in-memory close alone is insufficient for multi-worker deployment.

## Ego Browser Bridge Bindings

- Explicit same-identity re-enrollment updates release metadata through the canonical enrollment
  endpoint. Ordinary ensure and initial-request retries reject metadata drift; re-enrollment never
  substitutes for the separate key-rotation operation.
- `ego_browser_devices` and `ego_browser_bindings` form an independent identity and authorization
  domain for remote `ego-browser` heredoc execution. They do not reuse `devices`,
  `device_sessions`, GUI-control authorization modes, relay routes, credentials, or generations.
- A binding is created only by an authenticated ego-browser Device Client after the user selects
  one compatible tool session and confirms `ego_browser_script_full_trust`. The server derives the
  user, device, tool-session node, and platform identity and atomically enforces one live binding.
- The server also derives the binding's canonical Task Space label as
  `agent-remote:<tool_session_id>`. A claim may echo that value for transcript binding, but any
  mismatch is rejected and no client may choose another label. The canonical value is returned on
  claim and resume so the Device Client can validate its owner-only handoff; encrypted execution
  requests remain subject to the Bridge's exact label and scope checks.
- The bridge admits both `runtime_backend=native` and `runtime_backend=docker_sandbox` only when
  the assigned Node advertises the selected backend in its complete ego-browser capability report.
  Native uses the session's dedicated non-root identity. Docker Sandbox runs with the configured
  Node service UID/GID, mounts the broker socket and pinned artifacts into the container, and is
  admitted only after the Node broker verifies both the session nonce and the exact non-root
  `SO_PEERCRED` UID.
- The local Bridge is outbound-only. The browser relay validates a strict authenticated outer
  envelope and forwards opaque ciphertext; scripts, browser data, output, artifacts, URLs, and
  local paths are never parsed, persisted, audited, or logged by the control plane.
- Relay handshake validation and each frame admission use separate short database sessions.
  Close the handshake session before pairing or waiting on the network so device row locks
  cannot block the opposite role, lease renewal, or revocation.
- Relay tickets and Device proof challenges are one-time, role-bound where applicable, and consumed
  atomically from an independent Redis namespace. Production PostgreSQL deployments always use
  Redis-backed pairing: each worker publishes encrypted frames to an endpoint-specific channel and
  refreshes binding/generation/role presence with a five-second TTL. Duplicate role presence,
  missing subscribers, lost presence, malformed broker state, or Redis failure closes the relay;
  no worker falls back to process-local pairing.
- Binding revocation first commits a PostgreSQL outbox row. The cleanup publisher writes a
  20-minute old-generation marker before broadcasting over the Redis revocation bus. Relay workers
  atomically refuse marked presence and poll the marker while waiting or paired, so a missed
  Pub/Sub event still closes matching endpoints. Only successful marker and event publication marks
  the outbox delivery complete; repeated publication and close are idempotent.
- Physical deletion is a user-token or administrator cleanup operation, never a Device Client
  operation. A binding may be deleted only after it is terminal, has no active request, and every
  revocation outbox event has been delivered. A device may be deleted only after it is revoked and
  all of its retained binding history has been deleted; deletion audit records remain retained.
- Only `active/healthy` bindings admit execution. Lease renewal uses generation-aware compare and
  swap, a bounded failure grace, and an absolute TTL. Stop, revoke, policy drift, tool-session stop,
  node loss, device revoke, and user disable all invalidate the old generation before broadcast.
- A Device-authenticated pause carries a finite content-free reason. The service preserves
  `task_space_takeover` and `task_space_monitor_unavailable` in `stop_reason`, advances the
  generation, makes the old generation non-admissible, and publishes its revocation. The local
  Bridge must already have revoked admission and terminated managed executions before requesting
  this pause. Unknown reason text is normalized to `other` before persistence, audit, or outbox
  insertion. Only an explicitly confirmed Device resume may advance the paused binding again; it
  retains the server-derived Task Space label and never performs browser claim/takeover itself.
- File-allowlist and Site Learning capabilities are accepted only when their canonical roots
  digest or signed bundle digest is present and consistent. Production policy requires the full
  policy-backed capability set; development may negotiate only the independently verified subset.
- `ego_browser_bridge_enabled` defaults to false. Production enablement additionally requires a
  signed release-evidence manifest with project self-signing, Hardened Runtime, pinned certificate,
  owner-only credentials, application-enforced egress, SBOM, provenance, and compatibility
  evidence. Community releases explicitly remain non-notarized and non-public. Production startup
  loads that evidence even when only the Bridge flag is enabled, requires schema 9, and compares its
  published/readiness state, certificate, wrapper, Skill, protocol, runtime, learning-key, and
  artifact-digest identity with the deployment policy. Profile or certificate settings alone never
  authorize startup.
- Content-free relay and revocation metric events are documented in
  `docs/ego-browser-operations.md`. Metric labels have finite enumerated values and never carry a
  user, device, session, binding, request, URL, page, script, artifact path, or browser payload.

Runtime skill state and exact session snapshot persistence follow `docs/skill-runtime-state.md`.
The new relational layer does not itself enable managed sessions. Node content authorization must
be derived from a reserved session snapshot and the authenticated node/task binding; belonging to
the same node or knowing an object's digest never grants arbitrary user-library or account reads.

Skill lifecycle guards run before session deletion side effects. Single and bulk session deletion
reject pending skill state; durable retained finalization releases only the relational session ID
in the same deletion transaction. Managed account mode blocks the legacy launch path even when its
effective user library becomes empty. Account deletion additionally refuses runtime-state roots.

Node finalization ingestion is separate from preparation downloads and publication. It authorizes
the exact assigned snapshot after terminal process state, preserves the original termination
classification, and renews only expired upload attempts. Full input checkpoint persistence and
format diagnostics are atomic; invalid tool metadata cannot discard safe runtime bytes.

Account-local skill candidates and initial revisions are separate from the user library. Candidate
registration retains a source checkpoint but cannot activate content. Only active enabled local
identities enter exact session composition; their owner/account is rechecked at every lookup and
name collisions with enabled library items fail rather than selecting an arbitrary source.

Skill publication is separate from ingestion: the original Node's `/publish` request reauthorizes
its exact finalization, then uses the owner storage lock across current head reads, linked-unit merge,
new local candidate registration and final head CAS. Publication attempts and branch preconditions
retain conflict inputs. Any changed-branch epoch failure detaches the entire submission; unchanged
branches do not count as writes. Default revision changes do not redirect old-session writes. Actual
heads/local activation and the published receipt commit together. Conflict resolution and state
commands remain a separate user-authorized workflow; publication retry does not bypass a conflict.

User skill conflict queries and resolution commands use the existing user-token-only skill context.
They authorize the publication owner/account, label exact base/current/incoming sources, and retain
stable plan/idempotency revisions under the storage user lock. Dry-run does not save choices or
attempts. Stale targets are recomputed without transferring choices; only complete validated plans
may atomically change heads. Node publication requests never supply user resolution choices.

User checkpoint inspection uses the same private storage boundary as conflict queries. Checkpoint
IDs authorize only their owner, and list cursors additionally bind account plus item/directory scope.
Archived item identities remain addressable by stable ID; name collisions require an explicit ID.
Item exports retain the selected subtree and its complete connected link dependencies with original
paths, describing all additional roots rather than producing dangling links or silently empty output.
Checkpoint metadata distinguishes retained history/current heads from the source session's separate
finalization outcome. Pending uploads are listed separately and never presented as complete trees.

User reset/restore follows `docs/skill-state-mutations.md`: read-only effective selection feeds a
strict expected generation/directory/branch precondition, and the storage lock protects a single
savepoint containing all checkpoint/epoch/head/receipt writes. Previews never initialize state.
Superseding old plans does not execute them; later user resolution may explicitly recompute them.

Skill version preparation has a separate domain in `docs/skill-state-migrations.md`.
A successful exact snapshot reservation records the last effective library branch. Migration
preflight commits durable ready/conflicted outcomes before any session-admission rejection;
no synthetic sessions or finalizations may represent migration inputs. Complete directory CAS
and branch CAS share the user content lock and one rollback boundary. Public managed admission
orchestrates these services as specified below; it does not advertise Node capabilities.

Explicit skill migration accepts independently selected source/target revisions without changing
pins or effective-use history. Latest successful migration sequence supplies the delta baseline;
preview/conflict cannot advance it. First-use preparation and incremental migration share complete
branch/directory publication primitives, retaining distinct request and side-label contracts.

Migration conflict queries and exports use independent preparation identities and immutable saved
sides, as specified in `docs/skill-migration-conflicts.md`. Live heads and drift diagnostics never
replace saved comparison trees. The original account directory is labelled separately from the
migration current side. Read-only queries create no library, storage, branch or plan rows.

Migration resolution has its own content grants, plans, choices and immutable operation journal,
per `docs/skill-migration-resolution.md`. Scoped custom upload completion retains a verified tree
for exactly one migration; it does not resolve the conflict or advance any branch or migration
baseline. The full resolver must separately validate linked identities and publish atomically.

Migration candidate resolution is a pure four-input calculation: saved base/current/incoming plus
saved account-directory context. It computes complete connected units including reverse links in
the directory, preserves independent sources and never exposes a partial invalid manifest. A valid
candidate is not publication authorization; the orchestration layer must validate affected source
identities, heads, epochs, actual content and quotas before any atomic publication.

Internal migration plan edits replace overlapping choices under the user storage lock, validate the
entire resulting plan and CAS its revision together with a typed immutable draft receipt. A complete
candidate is reported separately from publication; this internal service creates no checkpoints and
has no public resolve route. Replay returns the original draft even after later edits or supersession.
Read-only previews never save choices, receipts or quota reservations. The separate internal final
resolver now authorizes related identities and combines plans, publication and final receipts atomically.

Checkpoint provenance is immutable creation evidence, separate from live branch state. New item
views record their original state epoch and exact backing directory when one exists; full directories
record their creation directory epoch. Reset/restore records the resulting epochs without rewriting
history. Legacy unknown provenance is explicit, never inferred from a digest or a reused state ID.
See `docs/skill-checkpoint-provenance.md`; migration authorization must verify these proofs separately.

Complete migration candidates now validate related stable identities against saved directory members.
Whole incoming selections additionally follow the source item's explicit backing directory and compare
historical member state IDs and epochs, including equal-byte members. Missing provenance fails
explicitly. Changed related branches must retain the saved head and an active source. This read-side
validation does not replace final multi-branch write-lock/CAS publication or stale recomputation.

Internal migration resolution now combines versioned plans, exact multi-branch publication, the saved
source checkpoint's success sequence and a typed immutable receipt in one user-locked savepoint.
Only changed related members get new item checkpoints; deleted items retain historical views while
leaving directory membership. Complete previews include each written branch's own/current/original
identity and changes. No original preparation response is rewritten. User routes are described below;
stale recomputation and replacement links follow the retained-input protocol below.

Initial and older preparation may conflict when replacing a directory member breaks a retained link.
Explicit resolution preserves those modes and leaves the migration success sequence null. Only
forward/incremental success advances the migration baseline. Initial input has no invented source
checkpoint; importing linked stable members without source provenance fails explicitly.

User migration resolve and typed operation lookup now route through the user-only skill context and
feature gate. Each accepted choice/receipt commits through the atomic resolver; dry-run is read-only.
Stale and superseded responses never apply a submitted choice. Retained-input recomputation and
replacement links use the protocol below; previews save no new comparison.

Migration stale handling follows `docs/skill-migration-recomputation.md`. Same-epoch head changes may
create one retained-input replacement without transferring choices. Epoch/identity loss terminates
the old plan instead of reviving cleared state; a newer successful baseline supersedes older inputs.
Original acceptance JSON stays immutable. Replacement links, invalidation, any clean publication and
the resolve receipt must share one owner write lock and savepoint; previews cannot save a replacement.

Skill migration lifecycle invalidation shares the originating library/publication savepoint.
Library changes evaluate exact per-account effective targets for preparation and installation
identity for every migration; successful forward/incremental publication supersedes only older
conflicts in its exact direction and epoch scope. Repository methods own these queries and updates.
Linked recomputation checks actual changed members of the retained candidate, while a fresh plan
uses current directory context for untouched members and preserves strict historical incoming
provenance. See `docs/skill-migration-recomputation.md` for candidate and rollback boundaries.

Managed tool-session admission is specified in `docs/skill-session-admission.md`. The session service
owns the outer savepoint and commit: natural migration conflicts commit retained preparation receipts
before returning a denial, while exceptions roll back all work. Skill services own backend capability
validation, account-wide first-use preparation and exact snapshot reservation; they do not bypass
session identity, node scheduling or existing system-component release gates. Node capability
advertisement remains disabled until its transfer/runtime implementation is verified.

Legacy configuration import ownership follows `docs/skill-config-import-exclusion.md`. Planning,
old-task start and fresh Node execution authorization check account directory mode under the user
content lock. Managed ownership remains enforced when the skill feature switch is disabled. The
Node checks the exact task response and the whole batch before writes. Account takeover must still
drain all old writers; an authorization response is not a takeover lock.

State reset/restore CLI recovery uses the existing immutable state-operation receipt. User-only
GET /skills/state/operations/{id} adds ID-based lookup alongside the original key query; both filter
by authenticated owner in the repository and return the original response without executing commands.
It does not claim Node deployment or introduce new operation persistence. Generic CLI status may
try this typed route only after a library operation is explicitly not found.

Resolution receipt recovery also supports owner-bound GET
/skills/state/resolution-operations/{id} and
/skills/state/migration/resolution-operations/{id}. These query existing immutable operations,
never execute a choice, create plans or recompute conflicts. Migration receipts keep the original
result separate from current supersession diagnostics. ID and original-key lookup return the same
receipt; the CLI may fall through domains only on explicit OPERATION_NOT_FOUND.

Incremental migration receipts also support owner-bound operation-ID lookup. Both key and ID
queries return the immutable original result separately from current supersession diagnostics.
The shared preparation ledger rejects non-incremental identities with OPERATION_KIND_MISMATCH;
lookup never reselects revisions or executes a migration. No schema change is required.

Skill retention reference analysis is specified in `docs/skill-retention.md`. Its repository reads an
owner-scoped, bounded index; service code builds semantic edges and the pure graph computes protection
reasons. Current directory membership is reported separately from true current-branch roots, so old
members require explicit directory compaction instead of permanent protection or unsafe deletion.
The inspector is read-only and internal; it does not authorize pruning or advertise completed GC.

Skill history release clocks use the explicit async retention mutation context described in
`docs/skill-retention.md`. Reference changes and clock transitions share the user storage lock and
savepoint. Read-only previews never initialize clocks; unknown legacy release times remain unknown.
This schema does not enable pruning or physical deletion. New reference writers must join that
context before changing any root, including runtime snapshot/session lifecycle writers.

Historical skill comparisons separate immutable digest evidence from retained content through the
0041 schema in `docs/skill-retention.md`. The internal account-bound retirement service validates all
selected identities, hard roots, waiting deadlines and parent histories before retiring any row.
It preserves audit receipts and valid incremental checkpoint baselines; expired comparison content
reads return STATE_EXPIRED even if another owner-local history still retains the same physical bytes.
This does not enable public prune, directory compaction, quota settlement or physical deletion.

Checkpoint retirement extends the same internal exact-history transaction using existing retained,
nullable tree_digest and branch expired fields. Retained comparison inputs, directory membership and
item backing contexts must retire together or block the selection. Retiring a historical branch head
atomically expires that branch while preserving its head identity. Explicit reset/restore can create
new content; ordinary selection/preparation cannot silently recreate expired learning data. Protected
exact checkpoint references fail closed after retirement. See `docs/skill-retention.md`; no public
prune, directory compaction or physical deletion is enabled by this internal step.

Internal skill directory compaction is specified in `docs/skill-directory-compaction.md`. Read-only
preview fixes exact account/checkpoint selection, current heads, backing membership, full manifests
and link dependencies. Apply rebuilds the plan under the retention user lock/savepoint before creating
new equivalent views and swapping all affected heads/members atomically. Old audit identities and
exact activity/baseline inputs remain untouched; no public prune, receipt, retirement or GC is implied.

Compaction retention preview uses a read-only graph overlay: virtual replacement checkpoints,
complete result-tree/object edges and current head/member substitutions. Original immutable history,
active snapshots, conflicts and exact migration baselines retain their original identities. No ORM
rows are modified for dry-run. Forecast release deadlines start at the fixed analysis time only for
newly unprotected identities; public prune must still revalidate dependencies, retire histories and
settle references transactionally. See `docs/skill-state-prune.md`.

Internal state-history retirement planning builds the full reverse consumer closure over retained
checkpoint, snapshot, finalization, publication and migration dependencies, including cycles.
Original local revisions and projected replacement references are explicit blockers, never implicit
state-prune targets. Preview and exact-plan apply share the existing user lock; the whole bounded
selection is validated before mutation rather than truncated to 1000-row batches. No public prune
route, durable request receipt, quota settlement or GC is implied. See `docs/skill-state-prune.md`.

Complete skill trees now join transactional release/reacquisition tracking. Fresh completed content
has an explicit unbound waiting origin; exact committed replay does not refresh it. Shared-file
admission checks user/digest across package and state categories, including existing-tree/package
reuse paths. This prepares two-phase GC without enabling deletion. See `docs/skill-tree-retention.md`.

Skill content reclamation follows `docs/skill-content-gc.md`: exact tree/object release and category
quota settlement share the caller's user lock/savepoint with durable deletion marking. A separate
worker owns independent transactions and only consumes committed task UUIDs; the user lock covers
physical I/O completion. Public prune scope/receipt integration remains separate from this internal
storage layer. No deletion may precede its durable mark or outlive its admission barrier.

The internal compaction/history/content composition follows `docs/skill-prune-projection.md`.
Read-only forecasts remove only selected historical content references and add every replacement
state-tree/object edge. Actual composition revalidates the original complete plan before its one
retention savepoint publishes heads, retires history and settles quota/deletion tasks. Public scope,
confirmation and durable receipt handling remain separate; no dry-run may mutate ORM state.

Account/item skill prune candidate planning follows `docs/skill-prune-candidates.md`. Canonical
stable-source scope and a fixed cutoff bind complete diagnostics, propagated dependency blockers,
whole executable groups and compaction-only waiting actions. Internal execution revalidates the
whole plan before one retention savepoint; it never skips failing groups during mutation. Public
confirmation digests and durable operation receipts remain required before exposing the command.

Public skill prune follows `docs/skill-prune-api.md`: read-only, digest-bound disclosure pages lead
to a user/scope/cutoff-bound final confirmation. Exact-key recovery precedes signature verification
or access to expired inputs. Immutable acceptance, complete disclosure rows and owner-constrained
deletion-task links commit with the internal prune transaction. Live deletion progress is separate.

User storage/retention diagnostics are specified in `docs/skill-storage-diagnostics.md`.
Read-only services combine owner-locked persisted quota/reservation counters, SQL deletion-task
aggregates and exact historical protection/deadline observations. They do not read content files,
change retention clocks or start cleanup. Existing detail views carry additive diagnostics while
original operation receipts remain immutable. Node disk usage is outside this Server observation.

`docs/skill-effective-queries.md` specifies read-only effective account and original session views.
Queries use saved session selection/system references, never current rules to reconstruct history.
Account detail selection does not initialize branches or dispatch preparation. System release catalog
metadata cannot claim runtime installation. Finalization records a dedicated immutable persisted_at
when complete content commits; updated_at is not synchronization evidence.

Managed snapshot preparation has its own exact-attempt renewal endpoint, independent of takeover
and finalization leases. It reuses snapshot authorization and existing task-row locking. Download
staging separates short authorization transactions from verified private disk copies and rechecks
snapshot authority before responding, permitting concurrent renewal/revocation. The Node owns its
renewal lifetime and must cancel preparation when authorization becomes uncertain. This endpoint
alone does not enable managed dispatch, runtime launch or capability advertisement.

Managed stop progress uses the original snapshot UUID and owner-scoped metadata reads,
independent of task result replay. See `docs/skill-stop-status.md` for the API and evidence boundary.

Native takeover dispatch uses the exact polled task record and attempt. The worker renews the
lease through Helper capture and upload, and never caches transient errors as task failure.
Generic completion accepts only the exact six-field committed takeover result after the initial
checkpoint transaction has committed; generic failure cannot consume a takeover reservation.
Exact replay has no directory lifecycle side effects. The original Helper capture and Server
reservation provide durable recovery even when the worker restarts before reporting completion.

Native session admission now reserves initial account takeover under the existing user content
lock when enabled library content first requires a legacy account. The original affinity Node and
pinned Native backend must match the selected healthy compatible Node; takeover never selects an
empty source on a replacement Node. The session transaction commits only the takeover reservation
and its task before returning MIGRATION_PENDING with an operation ID and explicit no-session
evidence. Repeated attempts reuse the original receipt; no legacy writer is stopped. Existing
migrating state without consistent original reservation/task evidence fails closed. Owner/device
GET /sessions/skill-takeovers/{operation_id} reads bounded metadata without renewing or creating work.
Only committed authority permits a fresh session-creation attempt. Docker takeover admission remains
unsupported until its independently verified writer adapter is integrated.

Library acceptance supersedes older unfinished deployment operations only when a common affected
account's saved effective plan differs. The operation replacement marker is independent of target
progress; late completion cannot clear it or release another active target's references. Comparison
and linkage validation live in deployment replacement services, and the existing user lock/savepoint
covers configuration, original plans, supersession and retention clocks together. Never reconstruct
legacy attempt history or treat a replacement marker as Node cancellation/drain evidence.

Deployment reservation is separate from session creation and effective use. The internal admission
and task authorization boundary in `docs/skill-deployment-dispatch.md` requires an additional backend
protocol before creating a task. Ordinary pending acceptance and bounded polling now use the saved
compatible report under explicit Server policy. Node advertisements remain disabled pending complete
backend acceptance. Generic task results cannot end these reserved tasks; dedicated success and
failure/cancellation/drain confirmations are implemented below.

Deployment content transport uses separate authenticated `/node/skill-deployments/{attempt_id}`
manifest/file/lease routes. It returns the original plan and full saved directory, not a session
snapshot. File verification releases database locks and repeats exact task/poll/configuration
permission checks before response publication. See `docs/skill-deployment-dispatch.md`.

Dedicated deployment result/inspection routes follow `docs/skill-deployment-results.md`. One original
Helper preparation receipt, task success, attempt readiness and release clocks commit atomically;
metadata-only inspection and exact replay do not reconstruct or republish current content.

Dedicated deployment termination follows `docs/skill-deployment-termination.md`: immutable Server
revocation precedes Helper drain and atomic failed/superseded confirmation. Original success and
revocation share user/task locks. No generic task result or timeout can stand in for this protocol.

Initial takeover publication resolves separately recorded deployment discovery boundaries in the
same transaction. Accepted plans remain immutable; execution plans append only the exact initial
local sources. Authorization, retry, replacement and retention use this explicit resolved selection.
See `docs/skill-deployment-discovery.md` and `docs/skill-deployment-scheduling.md` for the immutable
discovery and bounded ordinary scheduling contracts. Capability advertisement remains gated.

## Explicit passive migration recovery contract

Explicit passive backend migration recovery uses a separate administrator-selected request UUID
and exact original logical task ID. Server pins the original task-record UUID itself, preserves
that task/result, and creates recover_tool_account_runtime:<account UUID>:<request UUID>. The
payload is a version-1 binding of recovery task/record, original task/record, Node/user/account,
tool and source/target. Only a terminal original migration still owning the recovery-required
profile can admit a new recovery. Same-key replay is read-only; another key waits for the previous
recovery to end. User content locks serialize acceptance, fresh Node authorization and results.
The current active owner, affinity, legacy directory, no active sessions, source and profile must
still match. A live recovery task lease and exact poll attempt are required before Helper access
and first result acceptance. Success echoes the exact authorization and recovered=true; failure is
fixed content-free metadata. Original failure remains immutable; recovery success advances only
that original profile and preserves account disable. Failed recovery keeps admission closed.
The Helper operation recover_account_migration bypasses generic caches and uses existing
version-2 receipts. Same-boot recovery may settle completed metadata but cannot begin/copy/chown/ACL/
stop anything. Previous-boot recovery requires an already durable whole-migration succeeded receipt,
complete original copy/target phase and ownership evidence, no rollback, and repeated absence of
all recorded current units and cgroups. It rechecks the original account, backup, unchanged receipts
and current boot without rewriting old-boot metadata. A started old-boot migration cannot be promoted
from phase receipts. Missing/old-version evidence, failed/incomplete rollback, foreign/live services,
invalid account, managed fence or absent backup remain recovery-required. Completed target evidence
is rechecked even if the local whole result already says succeeded. This does not authorize
interrupted ownership repair, rollback attestation or backend advertisement.

Managed termination now distinguishes stopped-writer evidence from frozen-content evidence.
`capture-pending` accepts only original snapshot identity, retained exit classification and a fixed
capture diagnostic. Existing termination locking/revocation is shared; an absent digest cannot imply
local durability, content persistence or permission to delete. A later exact frozen observation may
advance saving while the original process-stop task result remains immutable.

Upload object membership uses the persisted declaration index in `docs/skill-upload-object-index.md`.
Repositories own batched index insertion and exact owner/upload/digest queries. The content service
owns canonical validation and existing lease/quota/deletion barriers. Node finalization file requests
use current metadata inspection after the same original identity/attempt authorization; they do not
invoke whole-user staging collection or load whole manifests on each file. Status, begin, collection
and complete retain their full responsibilities. This changes no public protocol or Node capability.
Retention and GC forecasts share one active-upload declaration interpreter. Indexed staged inputs
use complete counted digest references; legacy staged inputs retain canonical manifest validation.
Completed upload manifests are audit-only for this analysis. The SQL metadata guard and graph/row
bounds remain effective; a missing indexed reference aborts rather than weakening a lease root.

Per-file content reads use the existing normalized tree/object references as described in
`docs/skill-tree-downloads.md`. Repository queries preserve exact owned-tree membership and the
whole-tree cross-category deletion barrier; the service still takes the user lock and validates all
requested bytes. Node snapshot/deployment authorization and post-copy reauthorization remain.
Full manifest endpoints retain full canonical directory validation; no process authorization cache
or arbitrary digest download is introduced.

Read-only Node export continuation shares the existing `NodeExportService` authority check and
token signing domain. Its additive `/renew` endpoint returns a predecessor digest, current exact
permission and a bounded successor grant without database/content/task mutation. The original token
and snapshot binding remain mandatory throughout; fixed-expiry `/verify` remains compatible. See
`docs/skill-node-export.md` for the integrated gateway/Helper/CLI lifetime contract and live
acceptance boundary. Updated gateways require this Server endpoint; roll out Server before Node.


Passive recovery lease renewal uses a separate Node-authenticated POST at
`/node-api/tasks/{task_id}/runtime-migration-recovery-lease`. The exact original authorization
(binding and poll attempt) is mandatory. Under the existing user/task locks, renewal repeats all
current recovery eligibility checks and refuses expired, terminal, changed or foreign authority.
Only the existing task lease deadline changes, by at most 300 seconds; no task, receipt, phase or
account content is created. Reply timings describe a duration from Server time. Node subtracts the
whole request round trip, renews before expiry and cancels Helper work on any uncertain renewal.
This permits bounded long passive inventory checks without adding permission-repair authority or
changing the version-1 binding/result. A committed terminal result wins a concurrent renewal refusal.


## Explicit source restoration verification

`recover-runtime --verify-source` submits `action: "verify_source"`. Its immutable binding is
version 2 with exactly twelve fields, including that action. Default recovery retains version 1
and its original eleven fields; an absent action is never serialized as null. Request keys cannot
change actions. Authorization, renewal and successful results echo the exact versioned binding.
The Helper only verifies an already terminal whole-version-2 failed original with its pre-copy
baseline, immutable failure attestation, copied backup, target/source ownership intents and all
three successful source rollback phases. Repeated complete content/permission/parent checks and
writer quiescence are mandatory; previous-boot records additionally require current unit/cgroup
absence. Verification never repairs permissions or settles incomplete receipts.
Only a fresh matching successful verification marks the original Server profile `rolled_back`,
keeps the source backend and releases that profile's admission gate. Original task/result and
account disable remain unchanged. Failure retains the gate; terminal replay cannot change a newer
profile. This does not implement interrupted repair or enable any runtime capability.


## Explicit interrupted source repair

`recover-runtime --repair-source` submits `action: "repair_source"` and a version-3 binding with
exactly twelve fields. It is mutually exclusive with `--verify-source`; v1/v2 stay unchanged.
The current lease, request key, authorization, renewal and result pin the action. Server success
keeps the source backend, preserves original failure and account disable, and marks only the
original profile `rolled_back`. All existing ownership, inactivity and admission gates apply.
Helper requires a whole-v2 started original, durably completed copy, immutable baseline, exact
backup/content and repeated passive writer quiescence. Independent private repair intent, attempt
and completion records preserve and permanently fence original receipts. Synchronous permission
restoration runs under cancellable lifecycle exclusion; shared parents accept only original
traversal effects and attributable interrupted restoration. Completed retries only revalidate.
Previous-boot repair additionally requires absent recorded units/cgroups. No content, backup,
original outcome or runtime identity is rewritten, and no process writer is launched.
