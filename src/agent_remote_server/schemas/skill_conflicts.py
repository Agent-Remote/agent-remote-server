"""
给出冲突真实输入、明确分支来源和计划执行结果。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice


class SkillConflictSummary(BaseModel):
    """
    目录冲突明确标注范围，取代后仍可找到原始内容。
    """

    id: UUID = Field(description="发布尝试即目录冲突标识")
    account_id: UUID = Field(description="唯一归属账户")
    finalization_id: UUID = Field(description="完整原始收尾")
    attempt: int = Field(description="本次发布尝试编号")
    scope: Literal["account-directory"] = Field(
        default="account-directory", description="明确的完整目录范围"
    )
    status: str = Field(description="当前尝试结果")
    reason: str | None = Field(description="归档或取代原因")


class SkillConflictPage(BaseModel):
    """
    有界分页账户未解决和已取代的冲突。
    """

    items: list[SkillConflictSummary] = Field(description="当前页冲突")
    next_cursor: UUID | None = Field(description="下一页游标，空表示没有后续记录")


class SkillConflictInput(BaseModel):
    """
    区分原始会话物化树、发布比较树及完整输入，不使用含糊侧名称。
    """

    source: Literal["session_snapshot", "publication_comparison", "finalization"] = Field(
        description="内容真实来源类型"
    )
    reference_id: UUID = Field(description="来源对象身份")
    tree_digest: str | None = Field(description="完整状态树摘要，已无比较树时为空")


class SkillConflictBranch(BaseModel):
    """
    显示保存的精确目标及原快照固定的版本，不替换成当前默认版本。
    """

    state_id: UUID = Field(description="原始独立运行分支")
    entry_name: str = Field(description="当时实际暴露的目录名")
    state_epoch: int = Field(description="冲突时的目标状态纪元")
    checkpoint_id: UUID | None = Field(description="冲突时的目标 head")
    revision_id: UUID = Field(description="原始会话固定的来源版本")
    changed: bool = Field(description="原始输入是否真正修改该分支")


class SkillConflictView(SkillConflictSummary):
    """
    完整冲突说明与已保存选择，不包含文件正文。
    """

    session_reference_id: UUID = Field(description="原始会话身份，展示会话删除后仍保留")
    replacement_id: UUID | None = Field(description="已取代该尝试的最新发布结果")
    base: SkillConflictInput = Field(description="原始会话实际物化基线")
    current: SkillConflictInput = Field(description="冲突时固定的完整比较树")
    incoming: SkillConflictInput = Field(description="完整持久化会话输入")
    branches: list[SkillConflictBranch] = Field(description="精确目标分支及固定来源")
    conflicts: tuple[SkillMergeConflict, ...] = Field(description="原始冲突清单")
    plan_revision: int = Field(description="已保存计划版本，零表示尚无选择")
    choices: list[SkillResolutionChoice] = Field(description="已保存的明确选择")


class SkillConflictPathDiff(BaseModel):
    """
    三侧元数据差异，二进制与大文件只显示摘要、类型和大小。
    """

    path: str = Field(description="发现根的相对路径")
    base: SkillTreeEntry | None = Field(description="原始物化侧条目，空表示不存在")
    current: SkillTreeEntry | None = Field(description="当时当前侧条目")
    incoming: SkillTreeEntry | None = Field(description="完整提交侧条目")


class SkillConflictDiff(BaseModel):
    """
    元数据分页差异，与详情中的明确三侧来源对应。
    """

    publication_id: UUID = Field(description="已授权发布尝试")
    items: list[SkillConflictPathDiff] = Field(description="本页不同路径")
    next_cursor: str | None = Field(description="下一页相对路径游标")


class SkillResolutionView(BaseModel):
    """
    区分计划保存、可发布预览、已发布和目标重算。
    """

    publication_id: UUID = Field(description="请求针对的原始尝试")
    operation_id: UUID | None = Field(description="已保存的幂等命令回执，预览为空")
    status: Literal["pending", "preview", "published", "superseded"] = Field(
        description="本次明确结果"
    )
    plan_revision: int = Field(description="本次保存或预览所依据的计划版本")
    ready: bool = Field(description="完整计划是否通过发布前验证")
    choices: list[SkillResolutionChoice] = Field(description="本次完整解决选择")
    remaining: tuple[SkillMergeConflict, ...] = Field(description="仍需处理的完整冲突")
    result_tree_digest: str | None = Field(description="完整可发布结果摘要")
    result_checkpoint_id: UUID | None = Field(description="实际已发布完整目录")
    replacement_id: UUID | None = Field(description="目标改变后的替代尝试，预览不创建它")
    stale_reason: str | None = Field(description="阻止旧计划继续发布的原因")
