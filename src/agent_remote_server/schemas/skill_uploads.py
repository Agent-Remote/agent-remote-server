"""
定义安装包上传入口的受限清单和持久化进度。
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


class SkillPackageUploadRequest(BaseModel):
    """
    用户安装包上传计划，不接受任意账户运行状态绑定。
    """

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化幂等键"
    )
    manifest: SkillTreeManifest = Field(description="完整安装包清单")


class SkillUploadView(BaseModel):
    """
    可在连接中断后重新查询的原始上传计划。
    """

    id: UUID = Field(description="上传标识")
    status: Literal["staged", "committed", "expired"] = Field(description="内容上传状态")
    tree_digest: str = Field(description="预期完整树摘要")
    reserved_bytes: int = Field(description="仍被本计划占用的预留字节")
    expires_at: datetime = Field(description="上传租约截止时间")
    manifest: SkillTreeManifest = Field(description="原始完整清单")

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime) -> datetime:
        """
        恢复数据库无时区时间的 UTC 语义，保持首次响应与重试一致。

        :param value (datetime): 数据库租约截止时间
        :return datetime: 明确 UTC 时区的截止时间
        """
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SkillFileReceipt(BaseModel):
    """
    文件接收成功不代表完整包或库版本已提交。
    """

    upload_id: UUID = Field(description="所属上传计划")
    digest: str = Field(description="校验通过的文件摘要")
    created: bool = Field(description="是否新增磁盘对象")


class SkillTreeView(BaseModel):
    """
    当前用户已完整保存的私有内容树。
    """

    tree_digest: str = Field(description="完整树摘要")
    manifest: SkillTreeManifest = Field(description="已验证完整清单")
