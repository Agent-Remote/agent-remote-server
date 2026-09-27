"""
定义节点精确终止观察及其原样确认契约。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SkillTerminationIdentity(BaseModel):
    """
    固定进程终止的原始快照身份，不携带宿主路径或租约授权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID = Field(description="原始会话身份")
    task_id: UUID = Field(description="原始准备任务记录身份")
    initial_tree_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="原始准备目录树摘要")
    directory_epoch: int = Field(strict=True, ge=1, le=2**63 - 1, description="原始目录纪元")
    library_generation: int = Field(strict=True, ge=0, le=2**63 - 1, description="原始用户库代数")
    unclean: bool = Field(strict=True, description="原始退出异常分类")


CaptureError = Literal[
    "quota_exceeded", "insufficient_storage", "portability_error", "capture_failed"
]


class SkillTerminationRequest(SkillTerminationIdentity):
    """
    完整冻结后的原始输入，不改变已经确认的退出分类。
    """

    incoming_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="完整冻结输入树摘要")


class SkillCapturePendingRequest(SkillTerminationIdentity):
    """
    只确认原始进程停止和首次捕获故障，不声称已有持久快照。
    """

    capture_error: CaptureError = Field(description="固定且不含内容的捕获失败原因")
