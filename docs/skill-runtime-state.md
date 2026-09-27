# Skill runtime persistence

The reviewed root skill-manager design authorizes account state and exact session snapshots.
Migration 0028 adds the library-backed runtime persistence layer; migration 0030 extends it with
account-local identities and revisions. No account enters managed mode merely because these tables
exist.

- AccountSkillDirectoryState fixes owner/account/tool, takeover mode, directory epoch and a
  directory checkpoint head. The mode remains independent of the library becoming empty.
- AccountSkillState identifies one account/installation epoch/base revision branch, with its own
  state epoch and checkpoint head. Different accounts and reinstalled epochs never share heads.
- SkillCheckpoint retains a complete account-directory tree and optionally an item subtree view.
  Tree/object ownership is enforced by composite foreign keys. Item checkpoints belong to a
  specific branch; directory checkpoints and item heads cannot be interchanged. Retired checkpoints
  retain the audit digest but release their tree reference explicitly.
- SkillDirectoryMember records immutable directory-checkpoint membership and the corresponding
  branch checkpoints. Root auxiliary content remains in the complete tree, outside named members.
- SessionSkillSnapshot fixes the session/user/account/node, preparation task, backend, library
  generation, directory epoch, initial directory head and complete materialized tree. Its item rows
  fix actual materialized branch/checkpoint/epoch/name and rule resolution. Snapshot reservation and
  all content references must commit in one transaction under the existing user storage write lock.
- SkillFinalization binds one immutable incoming digest and clean/unclean classification to a
  snapshot. Upload-pending is distinct from a retained complete checkpoint, and unclean input can
  never use clean persisted/published/conflicted states.

The session's relational ID may be cleared only after lifecycle/retention checks; its original
reference ID survives. Foreign keys do not cascade skill state on session, account, node or user
deletion. A guessed digest or another task on the same node grants no access. Node authorization
must follow the exact snapshot's prepared tree or finalization upload, not all content of the user.

Public managed admission connects preparation and reservation under the capability gate described
in `docs/skill-session-admission.md`. Transfer and account takeover remain required before a real
runtime can support managed accounts. History references are not all permanent GC roots: retention explicitly retires metadata
and releases content pointers before deleting unreachable trees.

Reservation service boundary: an internal reservation receives the authenticated user, existing
session and exact `create_tool_session` task. The task payload must agree on user, account, session
and backend. It requires an already managed account directory; it never performs implicit takeover.
It holds the storage user lock throughout rule resolution, missing-branch initialization, complete
manifest composition and snapshot insertion. This serializes generation/head/epoch changes instead
of performing unlocked preparation with stale inputs. A retry returns the original snapshot and
rejects a substituted task. Node capability admission remains the caller's separate mandatory gate;
the public session service now applies it before preparation and again before reservation.

Composition replaces only known member namespaces. Disabled members are omitted from this session
but remain in the account checkpoint. Root auxiliary files survive. Each selected checkpoint is a
view of its full tree, so cross-item relative links are validated only after the complete selected
directory exists. Missing dependencies and name collisions block reservation. Initial package
objects are charged to state storage and verified before new branch/checkpoint/snapshot references
commit. No new schema row or successful reservation advertises backend capability.

Node preparation reads use `/api/v1/node/skill-snapshots/{snapshot_id}` and its `/files/{digest}`
child route with the exact `task_id` query parameter. They require Node credentials, the feature
flag and an active preparation lease. The response fixes all snapshot identities and the complete
manifest. Files are selected only from that manifest, copied with digest validation to private
bounded-memory staging, and streamed after the read transaction commits. Terminal/revoked state
prevents subsequent requests; an already authorized file response may finish from its private copy.
Finalization upload is a separate authorization path and is not granted by these read endpoints.

Lifecycle integration keeps legacy admission closed for any `migrating` or `managed_v1` account,
including an empty effective library; managed accounts use the gated snapshot admission path.
Session single/bulk deletion checks skill retention before
other revocation/deletion work. Pending snapshots return `STATE_PENDING`; only a retained snapshot
with a durable published/conflicted/detached finalization may release its relational session ID.
The original session reference, content, items and finalization remain. Account deletion retains
all previous guards and additionally refuses an account directory state reference. No deletion
path treats a stopped process as proof that its skill content reached Server.

Finalization ingestion adds a separate durable transfer binding in migration 0029. One immutable
finalization belongs to exactly one snapshot and records the incoming full-tree digest, original
session reference and clean/unclean classification. SkillFinalizationTransfer binds that receipt to
one current account-directory upload lease; composite foreign keys enforce the same owner, digest
and upload scope. Renewing an expired upload replaces only the transfer's lease and advances its
attempt number. It never changes the incoming digest/classification or creates another finalization.
Old upload IDs cease to authorize writes once replaced.

The assigned authenticated Node may begin finalization only after the Server session is stopped,
interrupted or failed. It must echo the snapshot's original session ID. This upload authority is
independent of the expired preparation task lease, so durable local recovery can finish after a
restart. Each request rechecks Node/snapshot/active-owner identity; request bodies cannot select
another owner, account or server path. The Helper remains responsible for proving writer quiescence
and sealing the first termination classification before requesting ingestion.

Begin fixes the complete manifest and classification with the idempotency receipt and quota lease
in one storage-user-locked transaction. Package limits do not truncate runtime data. The complete
directory limit and separate per-known-item/new-SKILL-directory/root-auxiliary limits apply before
accepting bytes. Final completion verifies every object, records the complete incoming directory
checkpoint plus surviving known item views and format diagnostics, and retains all references in
the same transaction. Invalid SKILL.md is retained as state, not rejected as an installation package.
Completion reports persisted or persisted_unclean and never advances any account or branch head.
Publication and conflict resolution remain separate work; the internal account-local candidate
registration service is available but is not called automatically by ingestion.

Quota recognition is two-stage for newly created directories: begin checks structural SKILL.md
candidates separately so valid new skills are not prematurely charged together as auxiliary data.
Completion verifies actual metadata and recomputes the aggregate root-auxiliary limit; invalid new
candidates remain auxiliary data and cannot split that quota. Previously known skills retain their
identity and independent limit even when SKILL.md becomes invalid. New-session composition also
checks the aggregate auxiliary quota before constructing its writable copy.

The Node endpoints are `POST /node/skill-snapshots/{id}/finalization`,
`GET /node/skill-finalizations/{id}`, `PUT /node/skill-finalizations/{id}/files/{digest}` and
`POST /node/skill-finalizations/{id}/complete`, under `/api/v1`. File writes and completion require
the current `upload_id` query parameter. Begin may renew only an expired attempt; get never extends
its lease. Bodies and file streams are bounded after Node authentication, and unknown owner/path
fields are rejected. These endpoints persist input only; managed session admission and Node
background upload/publication integration remain gated.

Directory publication merge units are derived from explicit stable member names supplied by the
publication service, never by guessing filenames as skill identity. Each known skill is a unit;
all anonymous root auxiliary paths form one unit. Ordinary relative links in the base, current and
incoming complete trees connect units, including intermediate symlink hops. Their union is retained
for the whole merge even if one side removes a link. Each connected unit uses conservative opaque
state rules independently, then the complete result is validated and returned atomically. A binary
change in an unrelated skill does not force another skill's disjoint text edit into conflict, while
linked database/script changes remain one indivisible conflict. Any conflict suppresses the entire
publishable result. Metadata authorization and epoch/head checks remain the publication transaction's
responsibility, not a property inferred from tree paths.

Account-local identity is introduced separately from the user library in migration 0030.
AccountLocalSkill belongs to one owner/account and records a stable source directory checkpoint/name.
Candidates remain staged until a publication transaction activates them; only active, enabled local
skills participate in later snapshots. Active names are unique inside the account. Distinct staged
candidates may share a name so concurrent creation is retained as an explicit source conflict rather
than silently conflated. A local skill has an immutable AccountLocalSkillRevision baseline view of a
complete state tree. Cross-entry links therefore keep their complete-tree validation boundary.

AccountSkillState supports exactly one origin: a user-library installation epoch/revision, or one
account-local skill/revision. Composite keys enforce the local owner/account/source relationship.
Local source identity is never injected into skill_installations, so user-library list/pin APIs
cannot accidentally expose or select another account's local revision. Local source epochs are
fixed to one; deleting/reintroducing a different local source uses a new stable identity, while
reset/restore advance the common branch state epoch. Candidate registration itself never changes
a directory head or activates a local item. Snapshot composition still rejects name collisions.

Downgrading 0030 is schema-reversible when no account-local identities exist. If local identities
have been created, downgrade refuses before any schema mutation: their state cannot be translated
into user-library installation rows without fabricating source ownership. Operators must restore a
pre-feature backup or explicitly export/remove those identities through retention first; downgrade
must not silently delete locally learned state.

The internal local-candidate service acquires the storage user lock, authorizes a retained complete
directory checkpoint, validates the shared installation name rules and reads actual SKILL.md bytes.
Retries reuse the same source checkpoint/name identity; concurrent retries also serialize. A new
candidate stores the complete source tree plus its top-level prefix, never a detached package with
broken cross-entry links. It changes neither library generation, directory heads nor activation.
Invalid new names are auxiliary data at ingestion, so they cannot split the auxiliary byte quota.

Snapshot reservation adds only active, enabled identities from its exact account. Resolution fields
carry account provenance and the local revision UUID. A library/local name collision raises
SKILL_SOURCE_CONFLICT. Expired local branches require explicit reset/restore; a new revision with
existing state requires migration. Current directory dependencies must still exist at composition:
retaining a complete original tree does not authorize copying historical auxiliary files into a new
snapshot. Candidate activation remains a required part of future publication and takeover services.

Publication persistence follows in migration 0031. A SkillPublication attempt belongs to one
owner/account/finalization and records its attempt number, terminal outcome, detach reason, target
directory epoch/head, comparison-tree reference, result directory checkpoint and structured conflict
list. The immutable base and incoming trees remain rooted by the original snapshot/finalization;
the comparison tree has its own owner/category foreign key. SkillPublicationBranch records the exact
exposed branch, expected state epoch/head, and whether that branch changed in the incoming tree.
Attempts are retained when superseded, so changing a target never erases unresolved input.

Initial publication runs under the same storage user lock as reservation and mutations. Unclean
input detaches. A directory epoch mismatch detaches the entire input; branch/source epoch invalidity
only detaches when that branch was changed relative to the exact snapshot. A default revision change
alone does not invalidate late writes to the snapshot's original branch. Head changes without epoch
changes are merged against fresh current state. New valid skill directories register staged local
identities; conflicting existing sources block the whole publication. Local activation, changed
branch heads, directory membership/head and finalization status commit atomically only after a
complete merge. Omitted snapshot entries are never treated as session deletions. A no-change input
receives a published receipt without advancing heads. Migration 0031 downgrade refuses while any
publication attempt exists, before dropping references needed to recover retained conflict input.

The initial publication service and authenticated Node `/publish` endpoint now implement this
transaction. A retry returns the same attempt and never retries over an unresolved conflict. A
changed directory head is projected with the snapshot's exact available branch heads; other current
members survive, so omitted entries do not become deletions. Comparison projections whose links
cannot compose are retained as a conflict using the actual current directory plus separately rooted
branch preconditions. Successful publication retains invalid/deleted known item views rather than
resetting them. New candidates are activated only if their merged result is still a valid skill.

The endpoint returns publication attempt ID, finalization ID, attempt number, status, detach reason,
result directory checkpoint and conflict count. It authorizes the original Node and terminal snapshot
again, independent of expired transfer leases. Content completion remains a separate endpoint and
never advances heads. Conflict resolution choices, superseding/recomputing plans and user state
inspection APIs are still required; the initial service deliberately returns existing conflicts until
that explicit workflow runs. Node transfer/cleanup acknowledgements are not yet integrated.

Resolution plans will retain each user choice separately from the immutable comparison inputs.
Choices cover a conflicting ordinary path, an entire connected merge unit, or the whole directory;
opaque/database units cannot be split into path choices. Custom files apply only when both sides
are ordinary files; structural/deletion choices select a complete side. Every choice is previewed
against base/current/incoming without content-level editing or conflict markers. An incomplete plan
never returns a publishable partial tree. Full composition, dependency validation, source identity,
quota and exact head/epoch checks are required again before applying a completed plan.

Migration 0032 adds SkillResolutionPlan (one owner/account/publication-bound monotonic plan
revision), SkillResolutionChoice (non-overlapping path/unit/whole selectors, explicit side or retained
custom state-tree foreign key), and SkillResolutionOperation (owner-scoped immutable idempotency
receipt and request digest). A dry-run does not save any plan, choice or receipt. Each command carries
the expected plan revision; retries replay their original response while changed requests cannot
reuse an idempotency key. Updating a selector removes overlapping old choices within the same user
lock. Referenced custom trees belong to the same user and remain GC roots until choices retire.

Resolving rechecks the directory and exact branch heads/epochs and current source validity. Stale
heads supersede the old attempt and recompute from the immutable original input with a new attempt
number; stale choices never transfer. Reset/reinstall invalidity yields a detached replacement.
Source conflicts cannot use path equality to take over another identity: an existing unexposed source
may only be retained unchanged, until an explicit source removal and recomputation makes the incoming
identity eligible. Completed custom results may not silently edit other unexposed current members.

The user inspection, scoped custom-content upload/export and transactional resolve endpoints are
implemented; their exact contract is recorded in `docs/skill-resolution-plans.md`. Real user-token
authorization, dry-run without writes, monotonic plans, immutable replay, stale recomputation and
atomic final application are covered on SQLite and PostgreSQL. Full choices also recheck previously
unchanged branches and cannot revive removed/reset sources or modify unexposed members. State
migration/retention and Node/CLI integration remain separate implementation work. Explicit reset/restore
is now implemented as described below.

## User checkpoint history and exports

`docs/skill-state-queries.md` specifies implemented user checkpoint list/info/member references,
explicit-checkpoint baseline diff, linked-unit tree/file export and separately paginated pending
uploads. Item lists include all revisions and installation epochs of one stable source; current-head
flags remain separate from source-session finalization outcomes. Finalization now retains empty item
views for known deletions so detached removal is also discoverable by skill, without adding absent
members to the incoming directory. Missing/corrupt files fail before successful streaming, and expired
content never becomes an invented empty tree. No schema change is required for this read layer.


## Effective state, reset and restore

`docs/skill-state-mutations.md` specifies read-only effective revision/branch selection, default state
diff and atomic preview/reset/restore operations. Migration 0033 retains immutable user-keyed state
operation receipts with owner/account/scope-constrained source/result references. Expected library
generation and complete directory/branch preconditions guard every command. New checkpoints and
selected epochs advance together; directory scope additionally advances the directory epoch while
unselected member heads remain unchanged. Old conflicts become superseded without automatic execution.
A later explicit resolve recomputes their original input, and invalid old-session writes detach.


## Last effective branch and first-use version preparation

Migration 0034 adds a same-account/installation/epoch effective-branch ledger that references actual
snapshot members. Only the end of a successful complete reservation updates it; retries of an old
snapshot, preview, preparation failure and late session publication cannot rewind it. Initial branch
reset without any reservation is not prior use. Historical snapshot members without a trustworthy
ledger require explicit recovery instead of guessing by timestamp or library revision order.

Independent version preparation saves exact source checkpoints and three labelled comparison sides.
It can initialize, conservatively migrate forward, initialize a never-used older revision with an
explicit warning, or resume an existing target head. All successful first-use target and directory
heads publish together. Pending conflicts retain their complete input references without fabricating
sessions/finalizations. Reset/restore supersedes those plans; preparation receipts stay immutable.
See `docs/skill-state-migrations.md` for routes and transaction ordering, and
`docs/skill-session-admission.md` for full-account orchestration. Migration-specific resolution is
specified in `docs/skill-migration-resolution.md`; explicit incremental migration is described below.

## Explicit incremental version migration

Migration 0035 extends the independent preparation journal with exact source baseline and target
current checkpoint references plus successful per-direction/per-epoch sequence numbers. The most
recent successful source checkpoint is the next delta baseline; failed/conflicted requests never
advance it. Compatible first-use forward history is backfilled as sequence 1 without rewriting its
immutable receipt. A reset changes the sequence scope rather than silently reusing an old epoch.

Explicit from/to selection may target non-effective or earlier revisions of the same current
installation without changing rules or effective-use history. Target comparison always uses its own
branch head. A repeated unchanged source preserves heads; later source changes cannot reintroduce
unchanged imported paths that the target deliberately deleted. Complete target/directory publication,
input references and success record share one savepoint and CAS boundary. Query and preview report
unmigrated source checkpoints and separate target-branch versus complete-directory changes.
