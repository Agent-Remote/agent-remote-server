"""
定义完整运行提交及可续期上传尝试的稳定响应。
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


class SkillFinalizationRequest(BaseModel):
    """
    节点提交其已冻结副本，不允许覆盖用户、账户或目录权限。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID = Field(description="必须匹配快照的原始会话身份")
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="节点持久化的收尾幂等键"
    )
    manifest: SkillTreeManifest = Field(description="完整冻结目录清单")
    unclean: bool = Field(strict=True, description="原始终止记录中的异常退出标记")


class SkillFinalizationView(BaseModel):
    """
    内容持久化状态不等于账户发布成功。
    """

    id: UUID = Field(description="唯一收尾提交标识")
    snapshot_id: UUID = Field(description="固定会话快照标识")
    incoming_digest: str = Field(description="原始完整输入树摘要")
    unclean: bool = Field(description="不可变异常退出标记")
    status: Literal[
        "upload_pending", "persisted", "persisted_unclean", "published", "conflicted", "detached"
    ] = Field(description="持久化与发布阶段")
    checkpoint_id: UUID | None = Field(description="完整保存后的输入目录检查点")
    upload_id: UUID = Field(description="当前有效上传尝试")
    upload_attempt: int = Field(description="单调递增的租约尝试编号")
    upload_status: Literal["staged", "committed", "expired"] = Field(description="当前上传租约状态")
    expires_at: datetime = Field(description="当前尝试的租约截止时间")

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime) -> datetime:
        """
        恢复数据库无时区时间的 UTC 语义，向跨语言客户端输出明确时区。

        :param value (datetime): 数据库上传租约截止时间
        :return datetime: 明确 UTC 时区的截止时间
        """
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
