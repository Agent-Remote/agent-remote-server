# Workspace synchronization state and authorization

A prepare task may publish active or failed only while its exact sync session is
starting and its task identity, node, user, workspace, device and remote path still
match. A late result never resumes a paused session. Publication uses a conditional
SQL update so a concurrent pause cannot be overwritten by a stale ORM object.

Resolve clears the conflict marker while retaining lifecycle state. Legacy healthy
records normalize to active during an explicit resolve operation; ordinary reads
remain read-only. This is metadata acknowledgement, not proof that files agree.
Clients must verify the actual Mutagen conflict state before resolving.

The additive node sync verification request accepts ssh_key_id from the trusted
SSH forced command. When supplied, it must identify an active key of that device;
the response echoes only the verified key ID. Updated gateways require this echo
and fail closed against old servers. Legacy gateways that omit the key retain
compatibility but do not obtain exact-key revocation protection; deployments must
upgrade Node as well as Server. No command text or key material is sent or audited.
Pause stops local synchronization when the client completes its Mutagen action;
it is not a revocation of existing transports or user filesystem access.

Updated CLI action requests include optional expected_target containing id,
workspace_id, node_id, local_path, remote_path, mutagen_session_id, remote_endpoint,
sync_mode, sync_git and exclude. All nullable fields are explicit. The Server locks
the current sync and assigned node, compares the entire target before any change,
and returns SYNC_TARGET_CHANGED (409) on drift. Status/conflict/timestamps are not
part of this identity. Resume/reset admission and preparation task changes commit
in one transaction. Omitted expected_target retains old client compatibility.

All sync data responses advertise integer target_precondition_version=1. Only a
successfully checked conditional action sets target_precondition_checked=true;
reads and legacy unconditional actions return false. New clients require version 1
before submitting any conditional action and require the checked flag in its receipt,
so an older server cannot silently ignore the additive request field.

Conflict and legacy healthy states still allow the restricted synchronization
transport so clients can rescan/flush before acknowledging conflicts. Failed and
stopped states continue to deny new sync transport authorization. This does not
relax ordinary tool attachment conflict checks.
