# Upload object declaration index

This implements the approved skill-manager design's complete-manifest, 100,000-entry uploads without
reparsing or transferring the entire manifest for every object. Migration 0054 adds
`skill_upload_objects`, keyed by `(user_id, upload_id, digest)`, with `tree_digest`, `scope` and one
validated `entry_json`. A composite foreign key binds owner/upload/tree/scope to the existing original
upload. `skill_content_uploads.object_index_version` is 0 for existing inputs and 1 for an atomically
completed index. No content bytes, credential, new upload attempt or additional retention root is
introduced. This is a derived declaration projection, not proof of received or committed content.

New begin operations validate the full manifest, calculate its original digest and reserve quota as
before, then insert the complete deduplicated file index and version in that same transaction. Legacy
version-0 uploads lazily build their index under the existing user write lock after full canonical
manifest/digest verification. Partial indexes cannot commit through this path. An indexed lookup
validates the selected entry again; missing entries cannot authorize writes. Complete still verifies
the original manifest identity and every declared object's complete bytes before publishing any
references. Terminal completion/expiration removes index rows while preserving original upload and
manifest audit metadata. Downgrade drops only this reproducible projection and its version marker.

Single-file operations load upload metadata without its large manifest and query only the exact
original digest. They recheck owner, active lease, quota category and deletion barriers under the
same user transaction lock. Node finalization file requests continue proving the exact original
Node/snapshot/session/attempt, but inspect its current lease without invoking whole-user staging GC.
Status/begin and collection retain their existing expiration/accounting work. No request renews an
expired lease, skips byte verification or treats one file as complete publication.

Retention analysis reads only upload metadata and indexed digest references for version-1 inputs.
`object_index_count` records the deduplicated count atomically with index version publication; every
staged index must contain exactly that many owner/upload-bound references or the whole analysis fails.
Rows consume the existing complete-index row budget. Entry JSON and canonical upload manifests are
not loaded for this graph. Version-0 staged inputs retain full-manifest validation and the existing
JSON budget; terminal upload manifests are audit data and never loaded as active roots. The existing
upload lease remains the sole root. This avoids rejecting one valid default-sized manifest against
the former 16 MiB aggregate metadata guard without removing or increasing that guard.
