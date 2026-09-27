"""
签发独立用途的短期导出凭据，不替代在线身份与撤销检查。
"""

import base64
import binascii
import hashlib
import hmac
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_remote_server.schemas.skill_node_export import NodeExportBinding
from agent_remote_server.services.skills.content_errors import SkillContentError

_DOMAIN = b"agent-remote:frozen-node-export:v1\x00"


class NodeExportGrant(BaseModel):
    """
    签名范围包含原用户凭据及精确设备密钥，不能跨用途或延长重放。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(default=1, ge=1, le=1, strict=True, description="固定导出授权版本")
    grant_id: UUID = Field(description="一次签发的随机身份")
    token_id: UUID = Field(description="必须保持活跃的原用户令牌身份")
    device_id: UUID = Field(description="授权设备")
    ssh_key_id: UUID = Field(description="授权公钥")
    binding: NodeExportBinding = Field(description="原始快照身份")
    issued_at: int = Field(ge=0, strict=True, description="签发时刻的 UTC 秒数")
    expires_at: int = Field(ge=1, strict=True, description="固定失效时刻的 UTC 秒数")


class NodeExportTokens:
    """
    私有协议域只接受有界签名载荷，错误不泄露原凭据。
    """

    def __init__(self, secret: str) -> None:
        """
        保存仅在内存中使用的部署密钥。

        :param secret (str): 部署签名材料
        """
        self._key = secret.encode("utf-8")

    def sign(self, grant: NodeExportGrant) -> str:
        """
        签发完整元数据字节，不存储原用户令牌或内容。

        :param grant (NodeExportGrant): 已完成权限检查的固定身份
        :return str: 有界原始凭据
        """
        payload = base64.urlsafe_b64encode(grant.model_dump_json().encode()).decode("ascii")
        signature = hmac.new(self._key, _DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
        value = payload + "." + signature
        if len(value) > 4096:
            raise ValueError("node export grant exceeds protocol bound")
        return value

    def verify(self, value: str, now: int) -> NodeExportGrant:
        """
        先校验精确字节，再检查固定用途和时间范围；后续仍需在线身份校验。

        :param value (str): 调用方回显的有界凭据
        :param now (int): 当前 UTC 秒数
        :return NodeExportGrant: 通过签名与时间校验的原始范围
        """
        try:
            if not 1 <= len(value) <= 4096:
                raise ValueError("invalid grant size")
            payload, signature = value.split(".")
            expected = hmac.new(
                self._key, _DOMAIN + payload.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii")):
                raise ValueError("invalid signature")
            raw = base64.b64decode(payload, altchars=b"-_", validate=True)
            grant = NodeExportGrant.model_validate_json(raw)
            if not grant.issued_at <= now < grant.expires_at <= grant.issued_at + 900:
                raise ValueError("invalid grant lifetime")
            return grant
        except (ValueError, UnicodeError, binascii.Error, ValidationError) as error:
            raise SkillContentError(
                "STATE_EXPORT_DENIED", "frozen export authorization denied"
            ) from error
