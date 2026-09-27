# Immutable takeover discovery for deployment

A configuration accepted before initial account takeover cannot know the manual sources that the
original Node will capture. Schema 0052 records this boundary separately from the immutable accepted
plan. `skill_deployment_discoveries` fixes the original target/plan digest and expected first takeover
directory epoch. It is created only during new plan acceptance for an unmanaged bound account;
existing operations are not backfilled and managed accounts never acquire this permission later.

The original takeover publication transaction resolves matching discoveries only for current active
or retryable attempts. Terminal unsupported/stored/ready and nonretryable failed history does not
expand when a later takeover publishes. Resolution
binds its exact original receipt and adds normalized, owner/account/revision-constrained initial local
sources from that receipt's initial directory. It never reads a later head or local default/enabled
selection to invent the original discovery. Resolved digests and entries are immutable. An empty
capture still records resolution. Original plan rows, attempt digests and public acceptance identity
remain unchanged; internal task/content plan digests identify the resolved execution selection.

Live authorization and retry compare current selection with this saved execution selection. A later
local disable/removal/revision change is real drift, not another discovery. Supersession comparisons
and retained replacement evidence use the same resolved selection. Pending operations protect the
original takeover directory and resolved local versions; terminal metadata alone does not retain
content forever. Existing active deployment tasks independently protect their complete input.

The receipt is supplementary configuration evidence, not readiness, a session snapshot or permission
to mutate running files. No Node capability or ordinary scheduler is activated by this migration.
Downgrade refuses recorded discoveries or sources rather than discarding their authority boundary.


The discovery-to-takeover foreign key includes user and account. The source-to-revision foreign key
also includes user, account and local source. Application validation additionally checks original
Node/backend, expected epoch and both plan digests. Retention validates each normalized addition
against its local source's original checkpoint and first revision, even after content becomes a
tombstone. Cross-account receipt/version substitution is rejected at the migrated database boundary;
receipt lookups are explicitly owner-scoped.

The wire shape remains deployment protocol version 1. Its `plan` and `plan_digest` describe the saved
execution selection; its original operation ID/generation and independent task/input binding do not
change. The unchanged accepted digest remains on the original operation target and attempt chain.
Root auxiliary content and unknown invalid skill directories remain part of the captured directory,
not invented source entries. Real source-name conflicts remain materialization errors.
