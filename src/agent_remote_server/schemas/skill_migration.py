"""
定义显式跨版本迁移选择、精确增量基线及可预览原子结果。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff


class SkillMigrationSelector(BaseModel):
    """
    两个版本都属于同一账户和稳定库来源，选择不改变生效规则。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID = Field(description="迁移所属账户")
    skill: str = Field(min_length=1, max_length=64, description="库来源名称或稳定身份")
    from_revision: str = Field(min_length=1, max_length=64, description="来源登记编号或版本身份")
    to_revision: str = Field(min_length=1, max_length=64, description="目标登记编号或版本身份")


class SkillMigrationBranch(BaseModel):
    """
    已授权精确版本的分支前置条件，不把未创建分支当成空发布。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_id: UUID = Field(description="已解析的不可变版本身份")
    state_id: UUID | None = Field(description="运行分支身份或尚未创建")
    state_epoch: int | None = Field(ge=1, description="当前运行分支纪元")
    checkpoint_id: UUID | None = Field(description="当前已发布检查点")
    expired: bool = Field(strict=True, description="是否需要先显式恢复")


class SkillMigrationPrecondition(BaseModel):
    """
    绑定双方状态与上次成功迁移，任一变化都要求重新预览。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID = Field(description="所属账户")
    skill_id: UUID = Field(description="稳定安装来源")
    name: str = Field(description="原始成员名称")
    installation_epoch: int = Field(ge=1, description="当前安装纪元")
    library_generation: int = Field(ge=0, description="当前规则代数")
    directory_epoch: int = Field(ge=1, description="当前目录纪元")
    directory_checkpoint_id: UUID = Field(description="完整目录 head")
    source: SkillMigrationBranch = Field(description="精确来源当前状态")
    target: SkillMigrationBranch = Field(description="精确目标当前状态")
    last_migration_id: UUID | None = Field(description="同方向同纪元上次成功迁移记录")
    last_migrated_checkpoint_id: UUID | None = Field(description="上次已合入目标的来源 checkpoint")
    last_sequence: int = Field(ge=0, description="上次成功序号，无历史为零")
    source_has_unmigrated_checkpoint: bool = Field(description="来源当前检查点是否尚未成功迁移")


class SkillMigrationRequest(BaseModel):
    """
    显式迁移必须携带完整双方预览和持久化幂等键。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: SkillMigrationSelector = Field(description="明确来源与目标版本")
    expected: SkillMigrationPrecondition = Field(description="用户已预览的完整状态")
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化命令键"
    )
    dry_run: bool = Field(default=False, strict=True, description="仅预览而不建立引用")


class SkillMigrationView(BaseModel):
    """
    明确标记增量基线与目标当前侧，冲突不暴露部分发布结果。
    """

    mode: Literal["incremental"] = Field(default="incremental", description="显式跨版本迁移")
    operation_id: UUID | None = Field(description="独立迁移受理身份")
    status: Literal["ready", "conflicted"] = Field(description="完整成功或保留冲突")
    before: SkillMigrationPrecondition = Field(description="受理前精确双方状态")
    base_source: Literal["old_original", "last_migrated"] = Field(description="共同基线的来源")
    current_source: Literal["target_original", "target_published"] = Field(
        description="当前侧的目标来源"
    )
    incoming_source: Literal["source_published"] = Field(
        default="source_published", description="输入为来源当前发布状态"
    )
    base_digest: str = Field(description="完整基线比较树摘要")
    current_digest: str = Field(description="完整目标比较树摘要")
    incoming_digest: str = Field(description="完整来源比较树摘要")
    result_tree_digest: str | None = Field(description="完整拟发布目录摘要，冲突为空")
    result_checkpoint_id: UUID | None = Field(description="完整成功目标检查点")
    result_directory_id: UUID | None = Field(description="完整成功目录检查点")
    migration_sequence: int | None = Field(ge=1, description="实际成功序号，预览或冲突为空")
    conflicts: tuple[SkillMergeConflict, ...] = Field(
        default=(), description="完整保留的冲突和关联单元"
    )
    changes: list[SkillStatePathDiff] | None = Field(description="目标分支自身变化，冲突为空")
    directory_changes: list[SkillStatePathDiff] | None = Field(
        description="当前完整目录变化，冲突为空"
    )


class SkillMigrationReceipt(BaseModel):
    """
    原始受理结果与后来的失效状态分别返回。
    """

    result: SkillMigrationView = Field(description="不可变原始结果")
    replacement_id: UUID | None = Field(default=None, description="当前替代比较或成功迁移身份")
    superseded_reason: str | None = Field(default=None, description="当前失效原因，原响应不改写")
    current_status: Literal["ready", "conflicted", "superseded"] = Field(
        description="当前迁移记录状态"
    )
