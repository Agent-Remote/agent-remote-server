"""解析并验证 ego-browser relay 的外层信封。"""

from __future__ import annotations

import base64
import binascii
import json

from agent_remote_server.ego_browser.relay.contracts import (
    EGO_BROWSER_PROTOCOL,
    EGO_BROWSER_RELAY_CHANNEL,
    EGO_BROWSER_RELAY_KIND,
)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_outer_envelope(raw: bytes, *, maximum_bytes: int) -> dict[str, object]:
    """
    解析并校验服务端可见的外层元数据。

    该函数只返回密文字符串，永远不会解码或检查其明文。

    :param raw (bytes): 原始外层信封字节
    :param maximum_bytes (int): 允许的最大信封字节数

    :return dict[str, object]: 通过协议、大小和规范编码校验的外层信封

    :raises ValueError: 信封超过大小限制或不符合外层协议与规范编码
    """

    if not isinstance(raw, bytes) or len(raw) > maximum_bytes:
        raise ValueError("frame_limit")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid_outer_json") from exc
    if not isinstance(value, dict):
        raise ValueError("outer_object_required")
    expected = {
        "protocol",
        "channel",
        "relay_binding_kind",
        "type",
        "request_id",
        "binding_id",
        "generation",
        "sequence",
        "direction",
        "payload_bytes",
        "nonce",
        "ciphertext",
        "auth_tag",
        "key_wrap",
    }
    if set(value) != expected:
        raise ValueError("outer_fields")
    if value["protocol"] != EGO_BROWSER_PROTOCOL:
        raise ValueError("outer_protocol")
    if value["channel"] != EGO_BROWSER_RELAY_CHANNEL:
        raise ValueError("outer_channel")
    if value["relay_binding_kind"] != EGO_BROWSER_RELAY_KIND:
        raise ValueError("outer_relay_kind")
    if value["type"] not in {"execute", "execute_result", "cancel"}:
        raise ValueError("outer_type")
    if value["direction"] not in {"request", "response"}:
        raise ValueError("outer_direction")
    if (value["direction"] == "request" and value["type"] not in {"execute", "cancel"}) or (
        value["direction"] == "response" and value["type"] != "execute_result"
    ):
        raise ValueError("outer_type_direction")
    for field in ("request_id", "binding_id"):
        item = value[field]
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ValueError(field)
    for field in ("generation", "sequence", "payload_bytes"):
        item = value[field]
        # Python 的 bool 是 int 子类，但协议整数不得接受布尔值。
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(field)
    encoded_lengths = {"nonce": 12, "auth_tag": 16}
    decoded_nonce = b""
    decoded_tag = b""
    for field, expected_length in encoded_lengths.items():
        item = value[field]
        if not isinstance(item, str):
            raise ValueError(field)
        try:
            decoded = base64.urlsafe_b64decode(item + "=" * (-len(item) % 4))
        except (ValueError, binascii.Error) as exc:
            raise ValueError(field) from exc
        if len(decoded) != expected_length:
            raise ValueError(field)
        if field == "nonce":
            decoded_nonce = decoded
        else:
            decoded_tag = decoded
    ciphertext = value["ciphertext"]
    if not isinstance(ciphertext, str):
        raise ValueError("ciphertext")
    try:
        ciphertext_bytes = base64.urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("ciphertext") from exc
    if len(ciphertext_bytes) != value["payload_bytes"]:
        raise ValueError("payload_bytes")
    # 重新编码可拒绝非规范 base64 及隐藏空白。
    for field, decoded in (("nonce", decoded_nonce), ("auth_tag", decoded_tag)):
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value[field]:
            raise ValueError(f"{field}_encoding")
    if base64.urlsafe_b64encode(ciphertext_bytes).decode().rstrip("=") != ciphertext:
        raise ValueError("ciphertext_encoding")
    key_wrap = value["key_wrap"]
    if not isinstance(key_wrap, str):
        raise ValueError("key_wrap")
    direction = value["direction"]
    if direction == "request" and value["type"] == "execute":
        if not key_wrap or not _is_canonical_b64url(key_wrap):
            raise ValueError("key_wrap")
        try:
            key_wrap_bytes = base64.urlsafe_b64decode(key_wrap + "=" * (-len(key_wrap) % 4))
        except (ValueError, binascii.Error) as exc:
            raise ValueError("key_wrap") from exc
        if len(key_wrap_bytes) != 92:
            raise ValueError("key_wrap")
    elif (direction == "request" and value["type"] == "cancel") or direction == "response":
        if key_wrap:
            raise ValueError("key_wrap")
    else:
        raise ValueError("outer_direction")
    wire_size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
    if wire_size > maximum_bytes:
        raise ValueError("frame_limit")
    return value


def _is_canonical_b64url(value: str) -> bool:
    """仅接受无填充且字符集合法的规范 base64url 文本。"""

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not value or any(char not in alphabet for char in value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        return False
    return base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value


class ApiEnvelopeError(ValueError):
    """调用方在 outer envelope 不满足 binding admission 时抛出的错误。"""
