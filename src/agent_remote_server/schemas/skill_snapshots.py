"""
定义节点精确准备快照的内容与身份封套。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule


class SkillSnapshotItemView(BaseModel):
    """
    实际物化成员的稳定分支与解析依据。
    """

    state_id: UUID = Field(description="账户分支标识")
    entry_name: str = Field(description="本次物化目录名称")
    state_epoch: int = Field(description="固定分支纪元")
    checkpoint_id: UUID = Field(description="固定起始检查点")
    resolution: ResolvedSkillRule = Field(description="服务端有效规则解析")


class SkillSnapshotView(BaseModel):
    """
    仅向绑定节点返回固定树和服务端确定的完整身份。
    """

    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID = Field(description="精确快照标识")
    user_id: UUID = Field(description="内容所属用户")
    account_id: UUID = Field(description="内容所属账户")
    node_id: UUID = Field(description="唯一准备节点")
    session_id: UUID = Field(description="原始工具会话标识")
    task_id: UUID = Field(description="精确准备任务数据库标识")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(description="固定运行后端")
    library_generation: int = Field(description="快照事务固定的用户库代数")
    directory_epoch: int = Field(description="固定账户目录纪元")
    starting_checkpoint_id: UUID | None = Field(description="原始目录检查点")
    tree_digest: str = Field(description="完整物化树摘要")
    manifest: SkillTreeManifest = Field(description="完整物化内容清单")
    items: list[SkillSnapshotItemView] = Field(description="实际暴露条目")
    system_releases: dict[str, object] = Field(description="固定系统技能版本引用")
