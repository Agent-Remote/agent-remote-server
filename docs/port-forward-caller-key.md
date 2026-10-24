# Port-forward caller keys

Migration `0061_port_forward_caller_keys` stores the optional caller request UUID and an intent digest on each forward, with a user-scoped unique index. Repeating the same key and intent reuses the existing non-terminal forward and issues a new one-time connection token. Reusing a key with a different intent returns `PORT_FORWARD_REQUEST_CONFLICT`; a settled forward returns `PORT_FORWARD_REQUEST_SETTLED`, and a different active device returns `PORT_FORWARD_REQUEST_DEVICE_MISMATCH`.

The key is metadata only. Connection tokens remain short-lived and are never stored in the desktop journal or exposed through the desktop safe projection.
