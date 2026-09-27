"""
向来源节点返回发布状态，不把冲突持久化误报成账户已更新。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SkillPublicationView(BaseModel):
    """
    精简发布回执，冲突详细内容由独立用户授权接口读取。
    """

    id: UUID = Field(description="发布尝试标识")
    finalization_id: UUID = Field(description="原始完整收尾标识")
    attempt: int = Field(description="发布尝试序号")
    status: Literal["published", "conflicted", "detached", "superseded"] = Field(
        description="完整发布结果"
    )
    reason: str | None = Field(description="整体归档或替代原因")
    result_checkpoint_id: UUID | None = Field(description="成功发布的完整目录检查点")
    conflict_count: int = Field(description="阻止整份提交发布的冲突数量")
