# 10 Identity And Authentication

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

- CLI device-code login must store only a hashed `device_code`.
- The `user_code` is short-lived and must expire.
- Completing CLI login before approval must fail.
- Completed or expired login codes must not be reused.
- The verification URL must route to the admin web approval view and include the user code.

## Devices And Keys

- Device registration is tied to the authenticated user.
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
