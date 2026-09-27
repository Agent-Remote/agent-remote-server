"""
定义迁移专用解决计划的只读视图，不把人工上传表示为发布。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff


class SkillMigrationResolutionPlanView(BaseModel):
    """
    原迁移的计划版本与当前失效状态分别展示，没有计划时版本为零。
    """

    migration_id: UUID = Field(description="原始独立迁移身份")
    current_status: Literal["ready", "conflicted", "superseded"] = Field(description="迁移当前状态")
    revision: int = Field(ge=0, description="当前计划版本，尚未创建为零")
    choices: list[SkillResolutionChoice] = Field(description="已经保存的完整选择，不包含正文")


class SkillMigrationResolutionDetails(BaseModel):
    """
    保存完整候选及目标原始包覆盖说明，发布状态由具体操作类型表达。
    """

    migration_id: UUID = Field(description="原始迁移冲突身份")
    operation_id: UUID | None = Field(description="持久回执身份，预览时为空")
    plan_revision: int = Field(ge=0, description="预览的当前版本或已保存的新计划版本")
    choices: list[SkillResolutionChoice] = Field(description="本次计算使用的完整非重叠选择")
    candidate_complete: bool = Field(description="候选是否完整有效，不表示已经通过发布授权")
    remaining: tuple[SkillMergeConflict, ...] = Field(description="仍需处理的内容冲突")
    unit: tuple[str, ...] = Field(description="保存四侧输入确定的完整关联范围")
    result_tree_digest: str | None = Field(description="完整候选清单摘要，不完整时为空")
    target_revision_id: UUID = Field(description="候选基于的确切目标原始版本")
    target_modified: bool | None = Field(description="相对目标原始包是否修改，不完整时为空")
    target_changes: list[SkillStatePathDiff] | None = Field(
        description="相对目标自身已保存当前侧的全部变化，不完整时为空"
    )
    original_changes: list[SkillStatePathDiff] | None = Field(
        description="相对目标原始包的全部覆盖，不完整时为空"
    )
    directory_changes: list[SkillStatePathDiff] | None = Field(
        description="相对保存账户目录的全部变化，不完整时为空"
    )
    other_changed_roots: tuple[str, ...] = Field(description="相对原账户目录发生变化的其他根")


class SkillMigrationResolutionDraftView(SkillMigrationResolutionDetails):
    """
    内部草稿始终不表示发布，保留独立类型供历史回执重放。
    """

    operation_kind: Literal["migration_resolution_draft"] = Field(
        default="migration_resolution_draft", description="独立迁移计划编辑操作类型"
    )
    status: Literal["preview", "planned"] = Field(description="预览或已保存计划，均不表示发布")


class SkillMigrationResolutionDraftReceipt(BaseModel):
    """
    原响应保持不可变，计划后续失效通过独立当前状态说明。
    """

    result: SkillMigrationResolutionDraftView = Field(description="最初接受的不可变计划编辑结果")
    current_status: Literal["ready", "conflicted", "superseded"] = Field(
        description="原迁移今天的状态，不改写历史编辑结果"
    )


class SkillMigrationResolutionBranch(BaseModel):
    """
    每个实际写入分支都说明原始版本、精确旧 head 和相对自身的全部变化。
    """

    name: str = Field(description="明确稳定来源根名称")
    state_id: UUID = Field(description="固定运行分支身份")
    skill_id: UUID = Field(description="稳定库或本地来源身份")
    origin: Literal["user_library", "account_local"] = Field(description="来源种类")
    revision_id: UUID = Field(description="分支固定的原始版本身份")
    installation_epoch: int = Field(description="固定安装纪元，本地来源为一")
    state_epoch: int = Field(description="本次发布保留的分支纪元")
    checkpoint_id: UUID | None = Field(description="比较使用的原 head，首次目标可为空")
    result_checkpoint_id: UUID | None = Field(description="完整发布的新单项视图，预览时为空")
    changes: list[SkillStatePathDiff] = Field(description="相对该分支自身当前侧的全部变化")
    original_changes: list[SkillStatePathDiff] = Field(description="相对该分支原始包的全部修改")
    modified: bool = Field(description="结果是否修改了该分支原始版本")


class SkillMigrationResolutionView(SkillMigrationResolutionDetails):
    """
    迁移完整发布与尚未完整的解决计划有明确不同结果。
    """

    operation_kind: Literal["migration_resolution"] = Field(
        default="migration_resolution", description="完整迁移解决操作类型"
    )
    status: Literal["preview", "pending", "published", "superseded"] = Field(
        description="只读预览、待补齐计划或完整发布"
    )
    replacement_id: UUID | None = Field(default=None, description="旧比较的替代尝试或成功记录")
    stale_reasons: tuple[str, ...] = Field(default=(), description="此次不执行选择的过期原因")
    recomputation_possible: bool = Field(
        default=False, description="是否属于可重新比较的同纪元变化"
    )
    result_checkpoint_id: UUID | None = Field(description="已发布精确迁移目标单项身份")
    result_directory_id: UUID | None = Field(description="已发布完整账户目录身份")
    migration_sequence: int | None = Field(
        description="仅完整发布的向前或增量迁移推进成功序号，初次或旧版准备为空"
    )
    affected: list[SkillMigrationResolutionBranch] = Field(
        description="完整候选实际将写入的所有分支"
    )


class SkillMigrationResolutionReceipt(BaseModel):
    """
    完整解决原响应与迁移今天的状态分别返回，查询不会重放发布。
    """

    result: SkillMigrationResolutionView = Field(description="最初接受的不可变解决响应")
    replacement_id: UUID | None = Field(default=None, description="迁移当前替代身份，不改写原响应")
    superseded_reason: str | None = Field(default=None, description="迁移当前失效原因")
    current_status: Literal["ready", "conflicted", "superseded"] = Field(
        description="迁移今天的独立状态"
    )
