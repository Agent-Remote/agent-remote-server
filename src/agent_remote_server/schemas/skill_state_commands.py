"""
定义当前有效状态、明确前置条件和可预览的重置恢复命令。
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_remote_server.schemas.skill_rules import ResolvedSkillRule
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff


class SkillStateSelector(BaseModel):
    """
    单项来源与完整账户目录互斥，不接受调用方指定所有者。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID = Field(description="当前用户所属账户")
    scope: Literal["item", "account-directory"] = Field(default="item", description="明确对象范围")
    skill: str | None = Field(
        default=None, min_length=1, max_length=64, description="单项名称或稳定身份"
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """
        完整目录不能混入单项选择，单项必须提供明确来源。

        :return Self: 已校验范围
        """
        if (self.scope == "item") != (self.skill is not None):
            raise ValueError("select one skill or account-directory scope")
        return self


class SkillStateTarget(BaseModel):
    """
    预览固定的精确来源、原始版本和可选运行分支。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(description="账户目录内的来源名称")
    skill_id: UUID = Field(description="稳定来源身份")
    origin: Literal["user_library", "account_local"] = Field(description="来源种类")
    revision_id: UUID = Field(description="当前规则选定原始版本")
    installation_epoch: int = Field(ge=1, description="当前安装纪元")
    state_id: UUID | None = Field(description="精确运行分支或尚未初始化")
    state_epoch: int | None = Field(ge=1, description="现有分支纪元")
    head_checkpoint_id: UUID | None = Field(description="当前分支检查点")
    expired: bool = Field(strict=True, description="是否必须显式重置或恢复")
    rule: ResolvedSkillRule = Field(description="启用与版本的逐字段解析原因")


class SkillStatePrecondition(BaseModel):
    """
    完整预览身份供后续请求比较，任一变化都不能静默覆盖。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    library_generation: int = Field(ge=0, description="规则配置代数")
    directory_mode: Literal["legacy", "migrating", "managed_v1"] = Field(description="账户管理模式")
    directory_epoch: int | None = Field(ge=1, description="现有目录纪元")
    directory_head_id: UUID | None = Field(description="完整目录当前检查点")
    targets: tuple[SkillStateTarget, ...] = Field(
        max_length=100000, description="按名称排序的完整选中集合"
    )


class SkillCurrentStateView(BaseModel):
    """
    当前选择不会创建分支，也不声称缺失分支已完成迁移。
    """

    selector: SkillStateSelector = Field(description="已授权选择范围")
    precondition: SkillStatePrecondition = Field(description="精确当前配置与状态")


class SkillStateCommand(BaseModel):
    """
    重置或恢复必须使用完整明确前置条件和独立幂等键。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化命令键"
    )
    action: Literal["reset", "restore"] = Field(description="显式状态操作")
    selector: SkillStateSelector = Field(description="单项或完整目录范围")
    expected: SkillStatePrecondition = Field(description="用户预览时的完整前置条件")
    checkpoint_id: UUID | None = Field(default=None, description="恢复使用的完整已保存检查点")
    dry_run: bool = Field(default=False, strict=True, description="只预览，不保存任何状态")

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """
        恢复必须提供来源，重置不能悄悄变为恢复。

        :return Self: 已校验命令
        """
        if (self.action == "restore") != (self.checkpoint_id is not None):
            raise ValueError("only restore requires a checkpoint")
        return self


class SkillStateBranchChanges(BaseModel):
    """
    每个真实分支相对自身旧 head 的变化，不能用当前目录内容代替。
    """

    skill_id: UUID = Field(description="受影响稳定来源")
    state_id: UUID | None = Field(description="旧精确分支或尚未初始化")
    checkpoint_id: UUID | None = Field(description="用于比较的旧分支 head")
    baseline_available: bool = Field(description="旧 head 内容是否仍可用于完整比较")
    changes: list[SkillStatePathDiff] | None = Field(
        description="相对该分支旧内容的变化，过期基线为空值"
    )


class SkillStateCommandView(BaseModel):
    """
    可确认的完整变更说明与不可变原始受理结果。
    """

    operation_id: UUID | None = Field(description="持久化操作身份，预览为空")
    status: Literal["preview", "published"] = Field(description="预览或完整发布")
    action: Literal["reset", "restore"] = Field(description="操作种类")
    before: SkillCurrentStateView = Field(description="提交前精确状态")
    result_tree_digest: str = Field(description="完整结果树摘要")
    result_checkpoint_id: UUID | None = Field(description="已发布完整目录，预览为空")
    changes: list[SkillStatePathDiff] = Field(description="相对当前完整目录的全部元数据变化")
    branch_changes: list[SkillStateBranchChanges] = Field(
        description="各真实目标分支相对自身旧 head 的变化"
    )
    affected: tuple[SkillStateTarget, ...] = Field(description="将推进纪元的精确分支")
    directory_epoch_advances: bool = Field(description="是否同时推进完整目录纪元")
    superseded_conflicts: int = Field(description="本次标记失效的未解决尝试数量")
