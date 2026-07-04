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

## CLI Login

- CLI device-code login must store only a hashed `device_code`.
- The `user_code` is short-lived and must expire.
- Completing CLI login before approval must fail.
- Completed or expired login codes must not be reused.

## Devices And Keys

- Device registration is tied to the authenticated user.
- Device tokens must be device-scoped.
- Revoking a device must make device tokens, SSH keys, and WireGuard peers unusable.
- SSH private keys and WireGuard private keys must never be accepted by user-facing APIs.

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
