"""
定义配置导入的精确执行授权，不在响应中携带文件或宿主路径。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SkillImportAuthorization(BaseModel):
    """
    当前有效任务绑定的账户目录模式。
    """

    task_id: str = Field(description="精确任务身份")
    node_id: UUID = Field(description="执行节点")
    user_id: UUID = Field(description="活动所有者")
    account_id: UUID = Field(description="任务目标账户")
    directory_mode: Literal["legacy", "migrating", "managed_v1"] = Field(description="当前目录模式")
    directory_epoch: int = Field(ge=0, le=2**63 - 1, description="目录纪元，无目录记录时为零")


class SkillImportAuthorizationResponse(BaseModel):
    """
    沿用节点 API 的数据和请求追踪封套。
    """

    data: SkillImportAuthorization = Field(description="新鲜的精确任务授权")
    request_id: str | None = Field(default=None, description="请求追踪标识")
