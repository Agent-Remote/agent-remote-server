"""
签名完整预览的规范范围、分页位置及确认阶段，不替代在线授权和真实计划重验。
"""

import base64
import binascii
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_remote_server.schemas.skill_prune import PruneBinding
from agent_remote_server.services.skills.content_errors import SkillContentError

_DOMAIN = b"agent-remote:prune-confirmation:v1\x00"


class PruneEnvelope(BaseModel):
    """
    有界签名载荷仅保存元数据，不嵌入大清单或完整损失列表。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = Field(default=1, description="签名协议版本")
    user_id: UUID = Field(description="签发时认证所有者")
    binding: PruneBinding = Field(description="全部原始确认条件")
    offset: int = Field(ge=0, le=10000000, strict=True, description="下一页或完整遍历结束位置")
    total: int = Field(ge=0, le=10000000, strict=True, description="完整披露行数")
    stage: Literal["page", "confirm"] = Field(description="继续展示或最终确认阶段")


class PruneTokens:
    """
    使用独立协议域限制凭据用途，解码失败不回显原始凭据。
    """

    def __init__(self, secret: str) -> None:
        """
        只在内存中持有部署签名材料。

        :param secret (str): 当前部署密钥
        """
        self._key = secret.encode("utf-8")

    def sign(self, envelope: PruneEnvelope) -> str:
        """
        对精确载荷字节签名，拒绝无法由协议有界传输的结果。

        :param envelope (PruneEnvelope): 服务端重验后的签发元数据
        :return str: 可持久化于用户原请求日志的紧凑凭据
        """
        payload = base64.urlsafe_b64encode(envelope.model_dump_json().encode()).decode("ascii")
        signature = hmac.new(
            self._key, _DOMAIN + payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        token = payload + "." + signature
        if len(token) > 4096:
            raise ValueError("prune confirmation exceeds protocol bound")
        return token

    def verify(self, token: str, user_id: UUID, stage: Literal["page", "confirm"]) -> PruneEnvelope:
        """
        先验证精确原字节再解析范围，其他用户和阶段都不能复用。

        :param token (str): 调用方回显的原始凭据
        :param user_id (UUID): 当前活跃认证所有者
        :param stage (Literal["page", "confirm"]): 本接口要求的阶段
        :return PruneEnvelope: 签名、所有者和阶段都有效的元数据
        """
        try:
            if not 1 <= len(token) <= 4096:
                raise ValueError("invalid token size")
            payload, signature = token.split(".")
            expected = hmac.new(
                self._key, _DOMAIN + payload.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii")):
                raise ValueError("invalid signature")
            raw = base64.b64decode(payload, altchars=b"-_", validate=True)
            envelope = PruneEnvelope.model_validate_json(raw)
            if envelope.user_id != user_id or envelope.stage != stage:
                raise ValueError("invalid token context")
            if envelope.binding.cutoff.utcoffset() is None:
                raise ValueError("invalid cutoff")
            if stage == "confirm" and envelope.offset != envelope.total:
                raise ValueError("incomplete confirmation")
            if stage == "page" and not 0 < envelope.offset < envelope.total:
                raise ValueError("invalid page position")
            return envelope
        except (ValueError, UnicodeError, binascii.Error, ValidationError) as error:
            raise SkillContentError(
                "INVALID_REQUEST", "invalid prune confirmation or cursor"
            ) from error
