# Ego Browser Relay Operations

This runbook covers only the independent `ego_browser_bridge` control-plane
domain. It does not use or depend on the GUI device-control product.

## Supported topology

Production requires PostgreSQL plus Redis and more than one Server worker may
accept either relay role. PostgreSQL deployments always select the Redis ticket
store, distributed relay hub, and revocation bus. The in-memory ticket store and
pairing path exist only for SQLite development tests.

Redis owns short-lived state only:

- one-use relay tickets and Device proof challenges are atomically consumed;
- role presence is keyed by binding ID, generation, and `bridge` or `wrapper`,
  expires after five seconds, and is refreshed while the endpoint is alive;
- encrypted frames and close notifications use endpoint-specific Pub/Sub
  channels;
- binding revocations write a 20-minute generation marker before using a
  separate cross-worker Pub/Sub channel. Presence registration atomically
  rejects a marked generation, and waiting or paired workers poll the marker.

The database remains authoritative. A lifecycle transaction increments or
terminates the generation and writes the revocation outbox before commit. The
cleanup loop republishes pending rows idempotently; a row is marked delivered
only after both the marker write and publication succeed. The marker outlives
the maximum relay ticket plus connection lifetime, so a worker that misses the
Pub/Sub event still closes or rejects the old generation. Redis errors, missing
peer presence, missing frame subscribers, malformed state, and publication
failures fail closed.

Only native Linux Claude sessions are eligible. A Docker session is returned in
candidate results with `controllable=false`, and a direct claim returns
`EGO_BROWSER_RUNTIME_UNSUPPORTED`.

## Binding and Task Space lifecycle

On claim, the Server derives `agent-remote:<tool_session_id>` and rejects a
different client-supplied Task Space label. It returns the canonical value on
claim and resume for the Device Client to validate and persist in its
owner-only handoff. The Node broker independently derives the same value for
one-use permits, and the Bridge rejects encrypted request labels or Task Space
scopes that do not match the bound value.

The local Bridge observes native ownership in an independent read-only monitor.
After that monitor has seen `ownership=agent`, a later
`agentDelegatedToUser` or `user` transition causes the Bridge to revoke local
admission, terminate managed executions, and only then submit a
Device-authenticated pause for the exact generation. The durable binding must
then be `paused` at the next generation with
`stop_reason=task_space_takeover`. An unavailable monitor follows the same
ordering and records `task_space_monitor_unavailable`. The Server preserves
these finite content-free reasons and normalizes unknown lifecycle text to
`other` before persistence; it does not infer takeover from helper errors and
never calls browser claim/takeover operations.

Recovery requires an explicitly confirmed Device resume at the paused
generation, which advances the generation but retains the canonical label.
Inspect current browser state before returning native ownership to the agent,
and never replay the interrupted request. If the Bridge cannot confirm its
pause request, local admission remains revoked; reconcile the Server binding
and outbox before issuing a fresh authorization.

## Content-free metrics

The Server writes JSON log events with `metric_name`, `metric_value`, and
`metric_unit`. It has no built-in Prometheus scrape endpoint. A collector may
convert these events to metrics, but it must attach `component=server` from
trusted workload metadata and must preserve only these finite labels:

| Metric | Event and finite labels |
| --- | --- |
| `ego_browser_bridge_connections` | Gauge on open and close; `metric_operation={opened,closed}`, `relay_role={bridge,wrapper}`, `relay_transport={redis,memory}`. Production is `redis`. |
| `ego_browser_bytes_total` | Counter contribution when a connection closes; `metric_direction={request,response}`, `metric_status={completed,rejected,frame_limit,transport_error}`, plus the finite role and transport labels above. |
| `ego_browser_revocations_total` | Counter after successful outbox publication; `revocation_reason={absolute_ttl,admin,policy,device_key,device,lease,node,pause,resume,tool_session,user,other}`. |

The event schema excludes user, device, tool-session, binding, generation,
request, URL, filename, local path, script, page, input, output, and artifact
content. Do not enrich a metric with those values.

Inspect canonical events without printing unrelated logs:

```sh
jq -c 'select(.metric_name == "ego_browser_bridge_connections") |
  {role:.relay_role,transport:.relay_transport,operation:.metric_operation,value:.metric_value}' \
  server.jsonl

jq -c 'select(.metric_name == "ego_browser_revocations_total") |
  {reason:.revocation_reason,value:.metric_value}' server.jsonl
```

After log-to-metric conversion, useful PromQL checks are:

```promql
max by (relay_role) (
  ego_browser_bridge_connections{component="server",relay_transport="redis"}
)

sum by (metric_status) (
  rate(ego_browser_bytes_total{component="server",relay_transport="redis"}[10m])
)

sum by (revocation_reason) (
  increase(ego_browser_revocations_total{component="server"}[15m])
)
```

## Health queries and alerts

Check durable lifecycle convergence directly:

```sql
SELECT count(*) AS pending, min(created_at) AS oldest
FROM ego_browser_revocation_outbox
WHERE delivered_at IS NULL;

SELECT status, lease_health, count(*)
FROM ego_browser_bindings
GROUP BY status, lease_health
ORDER BY status, lease_health;
```

Use `SCAN`, never `KEYS`, for a bounded Redis inventory. Do not fetch ticket,
challenge, or Pub/Sub payload values into incident logs:

```sh
redis-cli --scan --pattern 'agent-remote:ego-browser:*' | sed -n '1,100p'
```

Alert on these conditions:

- unmatched `bridge` versus `wrapper` connection gauges lasting longer than
  the pair timeout plus the five-second presence TTL;
- any sustained `transport_error` or `frame_limit` close rate;
- an outbox row older than three cleanup intervals (and at least 30 seconds);
- active/healthy database bindings with no paired relay and repeated Node
  `unavailable`, `renewal_required`, or `lease_expired` results for longer than
  the 20-second renewal interval plus 10-second grace;
- an active binding whose local service reports `task_space_takeover` or
  `task_space_monitor_unavailable`; it should converge to a paused next
  generation and zero old-generation relay presence;
- any `unknown_result`; this requires state inspection, never automatic replay;
- an unexpected increase in `policy`, `device_key`, `node`, or `other`
  revocations.

## Containment

1. Disable new claims with the deployment feature gate and roll the Server
   workers. This does not replace revoking already active generations.
2. Stop or revoke every live binding through the normal lifecycle service so
   PostgreSQL generation changes and outbox rows are retained.
3. Confirm that the outbox query reaches zero and both role gauges reach zero.
   If Redis is unavailable, leave rows pending; do not mark them delivered or
   delete relay keys by hand.
4. Stop the affected Node broker and local Bridge/Device Client services when
   immediate endpoint containment is required. Preserve metadata-only logs and
   terminal database rows.
5. Treat every interrupted request as `unknown_result`. Inspect browser state
   before any new action and never replay the original heredoc automatically.

## Recovery

1. Restore PostgreSQL and Redis health, then verify ticket `GETDEL`, Pub/Sub,
   presence expiry, revocation marker refusal, and subscriber health in a
   non-sensitive test.
2. Restart all Server workers so every worker has a fresh revocation
   subscription. Let the cleanup loop publish the durable pending outbox.
3. Verify zero stale outbox rows, balanced zero connection gauges, and no old
   binding generation in relay presence.
4. Deploy the exact compatible Server, Node, wrapper, Skill, Bridge, Device
   Client, and ego-browser runtime versions. Keep production admission disabled
   while the root release record says `production_ready=false`.
5. Require a fresh explicit native-session claim and generation, run
   `ego-browser --doctor`, then perform a bounded canary. Never restore an old
   ticket, permit, nonce, generation, or unknown request.
