# CLI remembered login

Server 0.2.23 and CLI 0.2.23 add remembered CLI user login. Access tokens keep their
normal one-hour lifetime. A separate rotating credential renews access during a
session whose absolute lifetime defaults to 30 days (`CLI_SESSION_TTL_SECONDS`).
Expiry, logout, token revocation, or account disablement prevents renewal.

| Endpoint | Credential | Result |
| --- | --- | --- |
| `POST /api/v1/auth/cli/session` | Valid ordinary user access token | Exchanges it for access/refresh credentials |
| `POST /api/v1/auth/cli/refresh` | JSON `refresh_token` | Consumes the current refresh credential and replaces both credentials |
| `POST /api/v1/auth/logout` | Current access token | Revokes access and its remembered session |

Refresh credentials never authorize ordinary APIs. Session access tokens cannot
create another session or use the legacy `/auth/refresh` route to extend its
lifetime. The dedicated refresh route is the only renewal path. Responses include
`access_token`, `expires_in`, `refresh_token`, and `refresh_expires_in`.

Migration 0025 adds keyed refresh hashes and stable session references, including
references on rotated access tokens so in-flight logout invalidates the current
session. Transactions serialize competing refreshes. The CLI serializes access to
its credential store across processes and replaces the complete credential pair
atomically. It does not retry an unknown refresh result or replay business actions.

Deploy Server and its matching root evidence bundle before upgrading CLI. Old CLI
and web logins retain their existing behavior. New CLI falls back to legacy tokens
when the Server lacks the session endpoint; expired legacy credentials still require
one login. Still-valid legacy credentials migrate on their next user command.
Normal device registration retains the user login alongside the device credential.

This login session never creates, resumes, or authorizes a browser binding. The
separate local full-trust confirmation remains required for connect/resume.

## Recovering a lost device-code completion

`POST /api/v1/auth/cli/start` accepts an optional caller-generated UUID
`completion_request_id`. The same UUID is sent to `POST /api/v1/auth/cli/complete`
and can be used with `GET /api/v1/auth/cli/completions/{completion_request_id}`
when the completion response is lost. The lookup is exact-key only and does not
accept a device code, user code, current-device listing, or latest-result guess.

The server persists only a short-lived Fernet-encrypted receipt. Neither the
plaintext `device_code` nor the issued token is persisted. Repeated completion
or recovery calls return the same active token receipt. Expired, revoked,
corrupt, missing, and otherwise unknown receipts fail closed; clients retain the
original UUID and must not start another login automatically.
