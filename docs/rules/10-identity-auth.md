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
- Revoking a device must make device tokens, SSH keys, and WireGuard peers unusable.
- SSH private keys and WireGuard private keys must never be accepted by user-facing APIs.
- An active device token may idempotently enroll or rotate that device's WireGuard public key.
- WireGuard enrollment audit records may contain peer IDs and allocation state, but not public key bodies.
- A device token may approve, lock, renew, reconnect, or device-stop only a device-control
  session whose `device_id` exactly matches the token's `user_device_id`.
- User tokens cannot replace local device approval. Device tokens cannot create a remote
  control request or change its user, tool session, node, platform, or expiry policy.
- Administrators may list all zero-content device-session metadata and force-stop a session, but
  cannot submit or replace local application approval.

## TOTP

- TOTP secrets must be encrypted before storage.
- TOTP setup may return the secret once for enrollment.
- Login must require a valid TOTP code when TOTP is enabled for the user.

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
