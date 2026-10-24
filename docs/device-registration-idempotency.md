# Device registration idempotency

`POST /api/v1/devices/register` accepts an optional UUID `request_id`. A client that
starts registration before a network boundary must retain this key and reuse it when
the HTTP result is lost. The key is scoped to the authenticated user and binds the
complete registration intent: name, platform, CLI version, SSH public key, WireGuard
public key, and the optional existing device ID.

The first accepted request stores an immutable intent digest and a short-lived encrypted
copy of the issued device token. The server stores only the encrypted token, never its
plaintext or a token-bearing audit field. Repeating the same key and intent returns the
same device, SSH key, peer, and token without rotating credentials or creating another
device. Reusing a key with a changed intent returns `409 DEVICE_REGISTRATION_KEY_REUSED`.

Clients may recover a lost response with:

```text
GET /api/v1/devices/registration-requests/{request_id}
```

The endpoint is read-only and user scoped. A missing key returns `404`; an expired or
revoked result is reported as `410 DEVICE_REGISTRATION_RECOVERY_EXPIRED` or `503
UNKNOWN_RESULT`. The recovery window is bounded by the issued device token expiry. A
client must preserve the original unknown outcome when recovery is unavailable and must
not generate a new key automatically.

Migration `0062_device_reg_requests` creates the durable user/key uniqueness
constraint and encrypted receipt columns. The table retains non-secret identifiers after
the device itself is removed only through the user foreign key lifecycle; it does not
authorize a new device or token.
