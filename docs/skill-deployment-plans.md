# Immutable skill configuration plans

Migration 0047 records the configuration inputs of each library operation's original account
targets. These are deployment inputs, not runtime snapshots or evidence of readiness. Capture
runs under the existing user storage lock and the same savepoint as configuration acceptance.
Idempotent replay returns the saved operation and never rebuilds its targets.

Global/tool-scoped mutations compare immutable before/after enabled source, epoch and revision
selections under the user lock. Unchanged accounts (including excluded sources and unaffected pins)
are omitted. Disable/remove still include previously enabled selections. Explicit account-scoped
commands retain their requested target, including reapplication. A no-op command may reapply its already-enabled sources
to expose pending migration conflicts; it never enrolls an account where that source is disabled.
Unfinished attempts for affected sources remain selected even while disabled, so new plans can supersede them
without dropping retained inputs. Full saved plans still include disabled sources; this filtering
does not rewrite historical receipts or change plan digests.

`skill_operations.plan_version` is nullable: historical operations have no reconstructable plan.
New operations use version 1, including operations with no targets. Each
`skill_deployment_targets` row records the original account, node, tool, backend and a digest of
the complete known effective library/local source selection. Pre-takeover discovery is separately
recorded in `docs/skill-deployment-discovery.md`; it never rewrites these acceptance rows. Account and node IDs are historical
identities, not cascading foreign keys; deleting or rebinding a live account cannot rewrite them.
The owning operation has an explicit composite foreign key.

`skill_deployment_entries` stores normalized selection fields: source kind and identity, selected
revision, immutable content digest, name, enabled flag, and library installation epoch. Composite
foreign keys enforce the same owner, installation epoch and revision, or the same local account,
source and revision. Entries include disabled sources so that a future retry can reproduce a
removal from the desired set. The complete desired set excludes already removed sources. Each
target's canonical digest includes its binding, operation identity and all sorted entries.

Pending and retryable operations retain both their original selected revisions and all plan
revision dependencies, including account pins. Successful/unsupported terminal receipts alone
are not permanent content roots. Superseded operations release their references under the same retention clock transaction only
after all original active targets have ended. Unknown plan versions, changed digests, missing targets or foreign
references fail closed before retention can authorize deletion. No historical plan is inferred
from today's configuration.

The [attempt ledger](skill-deployment-attempts.md) now preserves per-target history and internal
exact retry acceptance. The public `skill retry` workflow is connected; runtime dispatch remains
separate work. New configuration transactions compare saved effective selections and mark older
unfinished operations superseded only when an unfinished common target actually changes.
These records do not enable execution, advertise Node capability, or make unsupported targets
retryable. Downgrade refuses to discard recorded plans.


Schema 0052 fixes each newly accepted unmanaged target's expected initial takeover epoch. The exact
initial publication appends normalized local sources and a resolved execution digest in separate
tables, in the same transaction. Task/content authorization and retry consume that fixed execution
selection. Replacement comparisons use resolved selections on both sides, so discovery itself does
not make an unrelated later configuration into a false replacement. Original target/attempt digests
and user idempotency receipts remain unchanged. Existing operations are not backfilled.
