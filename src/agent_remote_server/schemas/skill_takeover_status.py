"""
向会话用户和设备提供不含文件及旧写入者清单的接管进度。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

TakeoverPhase = Literal["reserved", "uploading", "committed"]


class SkillTakeoverStatus(BaseModel):
    """
    原始预约阶段与任务状态分开，已预约不代表本地进程静止。
    """

    operation_id: UUID = Field(description="原始接管操作标识")
    account_id: UUID = Field(description="原始账户标识")
    status: TakeoverPhase = Field(description="原始接管阶段")
    task_status: str = Field(description="精确接管任务当前状态，记录缺失时为 missing")
    checkpoint_id: UUID | None = Field(description="原始初始目录检查点，提交前为空")
    recovery_required: bool = Field(description="是否缺少继续原任务所需的一致证据")


class SkillTakeoverStatusResponse(BaseModel):
    """
    复用现有会话响应封装，不授权写入或重新捕获。
    """

    data: SkillTakeoverStatus = Field(description="接管进度")
    request_id: str | None = Field(default=None, description="请求标识")
