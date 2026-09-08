"""提供 ego-browser 服务的无状态验证与规范化函数。"""

import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask
from agent_remote_server.services.ego_browser.contracts import (
    CONTENT_FREE_REASONS,
    REVOCATION_METRIC_REASONS,
    SAFE_DIGEST,
)

logger = logging.getLogger("agent_remote_server.services.ego_browser")


def _cancel_task_identity(task: NodeTask) -> tuple[UUID, int, str, int] | None:
    """严格读取 Server 自身创建的无内容 request 取消任务身份。"""

    payload = task.payload
    if task.task_type != "cancel_ego_browser_request" or set(payload) != {
        "binding_id",
        "generation",
        "request_id",
        "sequence",
    }:
        return None
    binding_id = payload.get("binding_id")
    generation = payload.get("generation")
    request_id = payload.get("request_id")
    sequence = payload.get("sequence")
    if (
        not isinstance(binding_id, str)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or any(ord(char) < 0x20 for char in request_id)
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
    ):
        return None
    try:
        parsed_binding_id = UUID(binding_id)
    except ValueError:
        return None
    return parsed_binding_id, generation, request_id, sequence


def _canonical_capabilities(values: Iterable[object]) -> list[str]:
    """返回去重、排序且有界的 capability 名称列表。"""

    result = sorted(
        {value for value in values if isinstance(value, str) and value and len(value) <= 128}
    )
    if len(result) > 32:
        result = result[:32]
    return result


def _as_utc(value: datetime) -> datetime:
    """归一化 SQLite 返回的不带时区数据库时间。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decode_public_key(value: str) -> bytes:
    """解码 canonical Ed25519 公钥。"""

    return _decode_fixed_key(value, "EGO_BROWSER_PUBLIC_KEY_INVALID")


def _decode_encryption_public_key(value: str | None) -> bytes | None:
    """在提供时解码 canonical 32-byte X25519 公钥。"""

    if value is None:
        return None
    return _decode_fixed_key(value, "EGO_BROWSER_ENCRYPTION_KEY_INVALID")


def _decode_fixed_key(value: str, code: str) -> bytes:
    """严格解码未填充的 32-byte base64url 公钥。"""

    # 重新编码可拒绝宽松解码器原本会接受的标准 base64 别名、填充和隐藏空白。
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ApiError(
            code=code,
            message="The ego-browser public key is invalid.",
            status_code=422,
        )
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ApiError(
            code=code,
            message="The device public key is invalid.",
            status_code=422,
        ) from exc
    if len(raw) != 32 or _encode_public_key(raw) != value:
        raise ApiError(
            code=code,
            message="The device public key is invalid.",
            status_code=422,
        )
    return raw


def _validate_outer_key_wrap(envelope: dict[str, object]) -> None:
    """只允许 execute request 携带 canonical key-wrap。"""

    direction = envelope.get("direction")
    message_type = envelope.get("type")
    value = envelope.get("key_wrap")
    if not isinstance(value, str):
        raise ValueError("key_wrap")
    if direction == "request" and message_type == "execute":
        if not value:
            raise ValueError("key_wrap")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("key_wrap")
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error) as exc:
            raise ValueError("key_wrap") from exc
        if len(decoded) != 92 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
            raise ValueError("key_wrap")
    elif (direction == "request" and message_type == "cancel") or direction == "response":
        if value:
            raise ValueError("key_wrap")
    else:
        raise ValueError("direction")


def _encode_public_key(value: bytes) -> str:
    """把固定长度公钥编码为未填充 base64url。"""

    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _verify_pop(
    *,
    public_key: bytes,
    challenge: str,
    signature: str,
    device_id: UUID,
    device_generation: int,
    operation_generation: int,
    release_profile: str,
    credential_profile: str,
    server_host: str,
    operation: str,
    binding_id: UUID | None,
    payload: BaseModel,
) -> bool:
    """校验绑定操作身份与 payload 的设备持有证明。"""

    try:
        challenge_bytes = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
        signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if len(challenge_bytes) != 32 or len(signature_bytes) != 64:
            return False
        message = bytearray(b"agent-remote/ego-browser/pop/v2\0")
        for value in (operation, str(device_id)):
            _append_pop_field(message, value)
        message.extend(device_generation.to_bytes(8, "big"))
        message.extend(operation_generation.to_bytes(8, "big"))
        for value in (
            str(binding_id) if binding_id is not None else "",
            release_profile,
            credential_profile,
            server_host,
        ):
            _append_pop_field(message, value)
        message.extend(challenge_bytes)
        message.extend(hashlib.sha256(_canonical_pop_payload(payload)).digest())
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature_bytes, message)
        return True
    except (
        ValueError,
        TypeError,
        binascii.Error,
        InvalidSignature,
        OverflowError,
        UnicodeEncodeError,
    ):
        return False


def _append_pop_field(message: bytearray, value: str) -> None:
    """向 PoP transcript 写入无歧义的 UTF-8 长度前缀字段。"""

    encoded = value.encode("utf-8")
    message.extend(len(encoded).to_bytes(4, "big"))
    message.extend(encoded)


def _canonical_pop_payload(payload: BaseModel) -> bytes:
    """序列化客户端实际提交且不含签名字段的 canonical payload。"""

    value = payload.model_dump(
        mode="json",
        exclude={"proof_challenge", "proof_signature"},
        exclude_unset=True,
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_reason(value: str) -> str:
    """把任意撤销原因归一化为有限且无内容的值。"""

    return value if value in CONTENT_FREE_REASONS else "other"


def _record_revocation_metric(reason: str) -> None:
    """记录一次已成功投递撤销的无内容指标。"""

    logger.info(
        "ego browser revocation metric",
        extra={
            "metric_name": "ego_browser_revocations_total",
            "metric_value": 1,
            "metric_unit": "revocations",
            "metric_operation": "published",
            "revocation_reason": REVOCATION_METRIC_REASONS.get(reason, "other"),
        },
    )


def _validate_digest(value: str | None, error: Callable[[str, str, int], NoReturn]) -> None:
    """校验可公开记录的 SHA-256 摘要格式。"""

    if value is not None and not SAFE_DIGEST.fullmatch(value):
        error(
            "EGO_BROWSER_DIGEST_INVALID",
            "The supplied bundle or allowlist digest is invalid.",
            422,
        )
