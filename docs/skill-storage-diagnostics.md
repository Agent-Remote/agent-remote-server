# User storage and retention diagnostics

Authenticated feature-gated `GET /api/v1/skills/storage` returns `SkillResult` with status `ready`,
committed=false, and a user-wide storage view. It reports current logical package/state bytes,
package/state upload reservations, the actual configured storage policy, and separately aggregated
physical deletion tasks (pending/retrying/completed counts, pending bytes and cumulative deleted
bytes). Counts never imply Node disk availability or the size of one skill. Completed byte totals sum the recorded sizes of completed deletion tasks and
are cumulative across deletion lifecycles: re-uploaded content may be counted again after a later
actual deletion. They are not free disk space. No filesystem paths or content bytes are returned.

Existing skill detail and checkpoint detail responses add optional `storage` metadata. Skill revision
rows and checkpoint detail also add optional `retention` metadata for their exact historical identity.
Lists may omit these diagnostics. The view records observed_at, kind/id, retained state, archived
classification, configured retention days, original release time, effective deadline and protection
reasons. States are protected, waiting, due, release_unknown and retired. A protected or retired
history has no effective deadline. A missing release clock remains unknown; reading never creates
one. Due means its own waiting period elapsed, not authorization to retire dependencies or delete
files. Complete prune review/revalidation remains required.

Services use the existing owner storage lock and complete retention graph, once per detail request.
All SQL aggregates remain in a dedicated repository. Reads do not create usage rows, increment lock
versions, expire uploads, write clocks, alter heads or invoke the deletion worker. Logical counters
and physical tasks share the existing lock protocol. The response is a current observation, never
inserted into an immutable operation receipt. No schema migration is needed.

The CLI adds `skill status --storage`, mutually exclusive with an operation ID, --last and --wait.
It consumes the bounded ordinary metadata API. Existing info commands render supplied diagnostics;
older Server responses lacking these optional fields remain valid and are never interpreted as zero
usage or infinite retention. The storage view explicitly excludes Node disk/copy usage.
