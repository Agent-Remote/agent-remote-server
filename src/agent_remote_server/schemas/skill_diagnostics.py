"""
区分当前用户逻辑配额、物理删除累计证据与历史等待观察。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillDeletionUsage(BaseModel):
    """
    物理任务单独计量，完成字节不是磁盘可用空间。
    """

    pending_tasks: int = Field(ge=0, description="尚待完成的物理任务数")
    retrying_tasks: int = Field(ge=0, description="至少尝试一次仍未完成的任务数")
    completed_tasks: int = Field(ge=0, description="累计已完成物理任务数")
    pending_file_bytes: int = Field(ge=0, description="当前待删除文件字节")
    cumulative_deleted_bytes: int = Field(ge=0, description="跨内容生命周期累计删除字节")


class SkillStorageView(BaseModel):
    """
    所有计量属于当前用户，不能解释为单项技能或节点的容量。
    """

    scope: Literal["user"] = Field(default="user", description="计量范围为当前用户")
    observed_at: datetime = Field(description="本次一致观察时间")
    package_bytes: int = Field(ge=0, description="逻辑计费的唯一发布包对象字节")
    state_bytes: int = Field(ge=0, description="逻辑计费的唯一运行状态对象字节")
    package_reserved_bytes: int = Field(ge=0, description="发布包上传预留字节")
    state_reserved_bytes: int = Field(ge=0, description="运行状态上传预留字节")
    policy: SkillStoragePolicy = Field(description="当前部署实际配置额度与保留期限")
    deletion: SkillDeletionUsage = Field(description="独立物理删除任务的汇总")
    node_storage: Literal["not_observed"] = Field(
        default="not_observed", description="不推断节点副本用量和磁盘余量"
    )


class SkillHistoryDiagnostic(BaseModel):
    """
    等待状态只解释单个历史身份，不能授予依赖组退役或文件删除权。
    """

    kind: Literal["revision", "local_revision", "checkpoint"] = Field(description="历史种类")
    id: UUID = Field(description="原始历史身份")
    observed_at: datetime = Field(description="引用保护与等待状态观察时间")
    retained: bool = Field(description="是否仍保留可恢复内容")
    state: Literal["protected", "waiting", "due", "release_unknown", "retired"] = Field(
        description="当前保护或等待状态，到期不等同删除授权"
    )
    protected_by: tuple[str, ...] = Field(description="当前有效保护理由")
    archived: bool = Field(description="是否按卸载归档期限计算")
    retention_days: int = Field(gt=0, description="本次实际采用的配置天数")
    released_at: datetime | None = Field(description="最后解除有效引用的已记录时间")
    expires_at: datetime | None = Field(description="无保护且有释放证据的有效等待截止")
