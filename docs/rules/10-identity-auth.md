# 10 Identity And Authentication

Exact session attach authenticates device tokens without writing last-seen timestamps. Both
authorization and observation require equality with the client's expected device before any
mutation. Node exact verification checks the active owner, device and actual SSH key and echoes
all bound identities; see [session attachment](../session-exact-attach.md).

## User Accounts

- The first administrator is created through the bootstrap API only when no users exist.
- Passwords must be hashed with Argon2id before persistence.
- User API responses must never include password hashes, TOTP secrets, tokens, or encrypted secret payloads.
- Administrator-only routes must use an explicit role check.

## Tokens

- Access tokens are opaque bearer tokens.
- Raw tokens are returned only once to the caller that requested them.
- Only keyed token hashes may be stored.
- Tokens must have an expiration time and a revocable status.
- Logout, refresh, device revoke, and device token rotation must update persisted token status.
- Device tokens use a separate long-lived TTL and clients must refresh them before expiry.

## CLI Login

- CLI user credentials can exchange a still-valid user access token at `/auth/cli/session`
  for a one-hour access token and a rotating refresh credential. A refresh credential is
  accepted only at `/auth/cli/refresh`, never as API authorization. Sessions expire after
  30 days by default without extending their absolute lifetime on refresh.
- The session points to its current access token. Refresh locks and rechecks both records,
  rejects expired sessions, disabled users and revoked access tokens, rotates the refresh
  hash, and revokes the previous access token atomically. Logout revocation therefore also
  prevents refresh. Generic `/auth/refresh` cannot rotate session-owned access tokens.
- Existing user tokens can migrate only while valid. CLI stores the complete credential
  pair atomically in its credential store and serializes refresh across local processes.
  Lost refresh responses fail closed; business mutations are never replayed automatically.
- CLI device-code login must store only a hashed `device_code`; a completion
  receipt may be retained only as short-lived server-keyed ciphertext. Plaintext
  device codes and access tokens must never enter persistent storage. Lost
  completion responses are recovered by the original caller UUID only, with no
  polling, latest-device inference, or replacement-code retry.
- The `user_code` is short-lived and must expire.
- Completing CLI login before approval must fail.
- Completed or expired login codes must not be reused.
- The verification URL must route to the admin web approval view and include the user code.

## Devices And Keys

- Device registration is tied to the authenticated user.
- Device registration callers may supply a UUID `request_id`. The server binds that
  user-scoped key to the complete registration intent and retains a short-lived encrypted
  token receipt. An identical retry or the read-only registration-recovery endpoint returns
  the original token without rotating it; a changed intent is rejected with
  `DEVICE_REGISTRATION_KEY_REUSED`, and an unavailable receipt remains `UNKNOWN_RESULT`.
- A CLI login may reuse only an active device owned by the authenticated user. Reuse rotates the
  device token and ensures the submitted SSH public key is active without creating another device.
- Device tokens must be device-scoped.
- Revoking a device must make device tokens, SSH keys, and WireGuard peers unusable. Revocation
  queues a device-scoped empty SSH key synchronization task for every node so stale
  `authorized_keys` blocks are removed even when the same public key was registered again.
- SSH private keys and WireGuard private keys must never be accepted by user-facing APIs.
- An active device token may idempotently enroll or rotate that device's WireGuard public key.
- WireGuard enrollment audit records may contain peer IDs and allocation state, but not public key bodies.
- A device token may authorize, lock, renew, reconnect, or device-stop only a device-control
  session whose `device_id` exactly matches the token's `user_device_id`.
- An active macOS device token may list the owning user's compatible running Claude sessions and
  claim one for its own device. Claim derives the user and device from the credential, validates
  the selected session again under transaction locks, and cannot change node, platform, runtime,
  generation, or expiry policy.
- User tokens cannot replace local session selection. The legacy user-token endpoint that accepts
  both `device_id` and `tool_session_id` is not a normal binding path and must remain disabled or
  explicitly restricted to audited migration compatibility. It must reject requests while the
  deployment policy is `session_full_trust`; only a capability-bearing Device claim may create that
  authorization.
- Administrators may list all zero-content device-session metadata and force-stop a session, but
  cannot submit or replace local session authorization.
- Revoking a device also revokes all of its non-terminal device-control bindings before the
  transaction commits and closes their relay pairs after commit.

## TOTP

- TOTP secrets must be encrypted before storage.
- TOTP setup may return the secret once for enrollment.
- Login must require a valid TOTP code when TOTP is enabled for the user.

## Ego Browser Devices

- Canonical `ensure` keeps existing keys, generation, and metadata unchanged. Explicit
  `re_enroll` may update verified release/runtime/policy metadata through the same idempotent
  endpoint, but must retain the exact signing key, encryption key, and device generation.
  It retains owner/origin/PoP checks and invalidates live policy generations when policy changes.
- Ego-browser devices use a separate credential namespace, device table, proof-of-possession key,
  encryption key, token hash, API routes, binding generation, and revocation path. They never use
  a general device token or `device_session` identity.
- Registration, key rotation, binding claim, Bridge activation, relay-ticket issuance, renewal,
  pause, resume, stop, revoke, and allowlist confirmation require a device-scoped credential plus
  a signed transcript bound to the exact request payload, operation, device ID and generation,
  binding ID and generation when applicable, release profile, credential profile, and control-plane
  hostname. Challenges are server-issued, short-lived, stored in the shared relay-state backend,
  and atomically consumed once so captures cannot be replayed across workers.
- Claim derives the user from the device credential and the node from the selected tool session.
  The request cannot substitute either identity, cannot auto-select a recent session, and cannot
  activate without explicit `ego_browser_script_full_trust` confirmation.
- Claim derives `agent-remote:<tool_session_id>` as the canonical Task Space label. A supplied label
  is transcript-bound but must equal that value. The Device Client validates the response before
  persisting its owner-only handoff, and a later resume preserves the same canonical label while
  advancing the generation.
- Native ownership takeover and ownership-monitor failure are reported only through a
  Device-authenticated, generation-bound pause after local admission has already been revoked. The
  bounded reasons `task_space_takeover` and `task_space_monitor_unavailable` are retained as
  lifecycle metadata. Resume requires fresh explicit full-trust confirmation; the Server does not
  claim or take over a browser Task Space.
- Allowlist confirmation is Device Client only, generation-bound, and compare-and-swap protected.
  User, Node, wrapper, administrator, and heredoc credentials cannot modify local roots.
- Revoking an ego-browser device first revokes every live browser binding and publishes the old
  generations. Raw credentials and private proof or encryption keys are never accepted or stored.

## Audit

Account binding attachment requires an active device owned by the account owner and
the exact expected binding attempt. Both POST and GET require explicit verified
Node gateway capability `binding_attach` with literal supported true and integer
protocol version 1; missing or incompatible support rejects before task creation.
POST may synchronize that device's SSH keys;
GET polls only the original task with fresh ownership, device, generation and payload
checks, never refreshing authorization by mutation. Node forced-command verification
must reject an expected attempt that differs from the current binding.
Exact binding verification also requires the SSH key identity supplied by the Node's
trusted forced command, checks current device ownership and active key status, and
echoes the verified key ID. Legacy binding requests without an expected attempt keep
their existing compatibility behavior.

Audit logs may include IDs, statuses, usernames, roles, and high-level action metadata.

Audit logs must not include:

- Raw passwords.
- Raw access tokens.
- TOTP secrets or codes.
- SSH public key bodies when an ID or fingerprint is enough.
- Private keys.
- Tool account login state.
- Browser cookies or browser profiles.
- Device-control application identifiers or digests, screenshots, input, clipboard data,
  window titles, coordinates, images, certificates, or connection secrets.

## Skill Manager

Read [Skill manager](../skill-manager.md) and its mandatory shared contract before changing Skill code.
Preserve user-scoped authorization, exact task/lease/snapshot bindings, atomic publication, reference
and retention transactions, and separate configuration/deployment/persistence receipts. Schema and
protocol changes require matching migrations, tests and callers; do not add stage-by-stage logs here.

Updated sync gateways supply the forced-command SSH key ID and require the verified
key echo. The server rejects revoked or foreign supplied keys; omitted keys retain
legacy compatibility only. See [workspace sync safety](../workspace-sync-safety.md).

Caller-key config-import receipt recovery uses read-only active-user/device authentication: ownership and revocation are rechecked, but device last-seen updates are suppressed. This does not weaken device validation or change legacy authentication behavior.

Reviewed runtime recovery routes require active user tokens with no device association and use
read-only credential validation. Authority returns admin=false for ordinary users; inspection and
request POST/GET require admin. Device-token rejection cannot refresh last-seen. Legacy admin
routes retain their existing behavior; see [the reviewed contract](../runtime-recovery-requests.md).
