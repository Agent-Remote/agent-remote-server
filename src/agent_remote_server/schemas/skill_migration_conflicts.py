"""
定义迁移专用冲突身份、不可变三侧和实时失效诊断。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_preparation import SkillPreparationView

type MigrationSide = Literal["base", "current", "incoming", "directory"]
type MigrationInputSource = Literal[
    "old_original",
    "new_original",
    "target_original",
    "last_migrated",
    "old_published",
    "source_published",
    "target_published",
    "existing_target",
    "account_directory",
]


class SkillMigrationConflictSummary(BaseModel):
    """
    原始迁移与会话收尾保持独立身份和来源语义。
    """

    id: UUID = Field(description="独立迁移受理身份")
    account_id: UUID = Field(description="所属账户")
    skill_id: UUID = Field(description="稳定库来源身份")
    installation_epoch: int = Field(description="受理时安装纪元")
    name: str = Field(description="受理时目标根名称")
    mode: str = Field(description="原始准备或增量迁移模式")
    status: str = Field(description="当前记录状态")
    source_state_id: UUID | None = Field(description="原始来源分支身份")
    source_epoch: int | None = Field(description="受理时来源纪元")
    target_state_id: UUID = Field(description="真实目标分支身份")
    target_epoch: int = Field(description="受理时目标纪元")
    directory_epoch: int = Field(description="受理时目录纪元")
    created_at: datetime = Field(description="原始受理时间")
    recomputed_from_id: UUID | None = Field(default=None, description="本比较重算自哪个原始尝试")
    replacement_id: UUID | None = Field(default=None, description="取代本尝试的新比较或成功迁移")
    superseded_reason: str | None = Field(default=None, description="原尝试失效原因")


class SkillMigrationConflictPage(BaseModel):
    """
    有界列出同账户未解决与已失效的迁移冲突。
    """

    items: list[SkillMigrationConflictSummary] = Field(description="本页冲突摘要")
    next_cursor: UUID | None = Field(description="下一页原始受理身份")


class SkillMigrationInput(BaseModel):
    """
    保存的比较树有真实版本身份，目录上下文单独标记。
    """

    source: MigrationInputSource = Field(description="输入的真实来源类型")
    revision_id: UUID | None = Field(description="所属原始版本，目录上下文为空")
    checkpoint_id: UUID | None = Field(description="来源检查点，原始包为空")
    tree_digest: str | None = Field(description="精确保存树摘要，已过期目录为空")


class SkillMigrationLiveBranch(BaseModel):
    """
    实时诊断保持原分支身份，不切换到今天的生效版本。
    """

    state_id: UUID = Field(description="原分支身份")
    revision_id: UUID = Field(description="不可变原始版本身份")
    epoch: int = Field(description="当前分支纪元")
    checkpoint_id: UUID | None = Field(description="当前分支已发布检查点")
    expired: bool = Field(description="当前分支是否过期")


class SkillMigrationDrift(BaseModel):
    """
    来源仅新增状态与需要重新计算的变化分别说明。
    """

    source: SkillMigrationLiveBranch | None = Field(description="原来源分支当前状态")
    target: SkillMigrationLiveBranch = Field(description="原目标分支当前状态")
    directory_mode: str | None = Field(description="当前账户目录管理模式")
    directory_epoch: int | None = Field(description="当前目录纪元")
    directory_checkpoint_id: UUID | None = Field(description="当前目录检查点")
    library_generation: int = Field(description="当前配置代数")
    installation_epoch: int = Field(description="当前安装纪元")
    installation_removed: bool = Field(description="稳定来源是否已移除")
    last_migration_id: UUID | None = Field(description="当前纪元范围最新成功迁移")
    source_head_advanced: bool = Field(description="来源同纪元出现更新，不替换保存输入")
    recomputation_reasons: tuple[str, ...] = Field(description="旧尝试需要重新计算的明确原因")


class SkillMigrationConflictView(SkillMigrationConflictSummary):
    """
    原始响应不可变，当前状态和漂移诊断独立展示。
    """

    original: SkillMigrationView | SkillPreparationView = Field(description="不可变原始受理结果")
    base: SkillMigrationInput = Field(description="保存基线及其真实来源")
    current: SkillMigrationInput = Field(description="保存目标当前侧及其真实来源")
    incoming: SkillMigrationInput = Field(description="保存来源输入侧及其真实来源")
    directory: SkillMigrationInput = Field(description="当时完整账户目录上下文")
    live: SkillMigrationDrift = Field(description="实时状态诊断，不修改原始输入")


class SkillMigrationPathDiff(BaseModel):
    """
    保存三侧的原始元数据差异不表示已批准写入。
    """

    path: str = Field(description="原始树内相对路径")
    base: SkillTreeEntry | None = Field(description="保存基线条目或不存在")
    current: SkillTreeEntry | None = Field(description="保存目标侧条目或不存在")
    incoming: SkillTreeEntry | None = Field(description="保存来源侧条目或不存在")


class SkillMigrationConflictDiff(BaseModel):
    """
    差异游标绑定本次尝试及三侧，不接受其他尝试的路径游标。
    """

    migration_id: UUID = Field(description="已授权迁移受理身份")
    comparison: Literal["saved_inputs"] = Field(
        default="saved_inputs", description="仅比较保存输入"
    )
    items: list[SkillMigrationPathDiff] = Field(description="本页原始三侧差异")
    next_cursor: str | None = Field(description="绑定本记录和三侧摘要的下一页游标")


class SkillMigrationTree(BaseModel):
    """
    精确导出原始完整树并明确额外上下文根，不裁剪或重写链接。
    """

    migration_id: UUID = Field(description="已授权迁移受理身份")
    side: MigrationSide = Field(description="保存侧或原始账户目录")
    input: SkillMigrationInput = Field(description="该侧真实来源")
    target_root: str = Field(description="迁移针对的目标根名称")
    extra_roots: tuple[str, ...] = Field(description="完整树额外包含的上下文根，不代表批准写入")
    manifest: SkillTreeManifest = Field(description="未改写的完整原始树")
