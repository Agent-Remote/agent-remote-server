"""
定义首次进入有效版本的可预览请求和三侧含义明确的结果。
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_state_commands import (
    SkillStatePrecondition,
    SkillStateSelector,
)


class SkillPreparationRequest(BaseModel):
    """
    明确选择当前生效单项，完整前置条件防止迁移到意外版本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化受理键"
    )
    selector: SkillStateSelector = Field(description="当前有效单项来源")
    expected: SkillStatePrecondition = Field(description="预览时完整当前配置")
    dry_run: bool = Field(default=False, strict=True, description="是否仅预览而不保存引用")

    @model_validator(mode="after")
    def single_item(self) -> Self:
        """
        此入口不把单项迁移误报成完整账户准入。

        :return Self: 已校验单项请求
        """
        if self.selector.scope != "item":
            raise ValueError("branch preparation requires one selected skill")
        return self


class SkillPreparationView(BaseModel):
    """
    三侧清楚区分旧原始包、新原始包与旧发布运行状态。
    """

    migration_sequence: int | None = Field(
        default=None, ge=1, description="该分支方向及全部纪元内的成功迁移序号"
    )
    operation_id: UUID | None = Field(description="独立准备记录身份，预览为空")
    status: Literal["ready", "conflicted"] = Field(description="完整可用或需要解决迁移")
    mode: Literal["initial", "forward", "older", "resume"] = Field(
        description="初始、向前迁移、进入旧版或复用既有分支"
    )
    before: SkillStatePrecondition = Field(description="受理时完整目标前置条件")
    source_revision_id: UUID | None = Field(description="最后实际使用的来源版本")
    source_checkpoint_id: UUID | None = Field(description="旧分支本次确切已发布状态")
    source_epoch: int | None = Field(description="本次来源分支纪元")
    target_state_id: UUID | None = Field(description="目标分支身份，未创建预览可为空")
    result_checkpoint_id: UUID | None = Field(description="完整成功目标检查点")
    result_directory_id: UUID | None = Field(description="成功后的完整目录检查点")
    result_tree_digest: str | None = Field(description="完整拟发布或已发布结果摘要")
    base_source: Literal["old_original", "target_original", "existing_target"] = Field(
        description="共同基线内容的真实来源类别"
    )
    current_source: Literal[
        "new_original", "target_original", "existing_target", "target_published"
    ] = Field(description="当前侧内容的真实来源类别")
    incoming_source: Literal["old_published", "target_original", "existing_target"] = Field(
        description="输入侧内容的真实来源类别"
    )
    base_digest: str = Field(description="旧原始包带成员前缀的完整比较树，初始或复用时为目标树")
    current_digest: str = Field(description="新原始包带成员前缀的完整比较树，复用时为目标树")
    incoming_digest: str = Field(
        description="旧发布成员比较树，关联冲突时保留其完整目录，复用时为目标树"
    )
    conflicts: tuple[SkillMergeConflict, ...] = Field(
        default=(), description="全部冲突及共同解决单元"
    )
    warnings: tuple[Literal["newer_state_not_migrated"], ...] = Field(
        default=(), description="未迁移新版运行状态的明确提示"
    )


class SkillPreparationReceipt(BaseModel):
    """
    原始受理结果不可变，失效标记在回执外单独展示。
    """

    result: SkillPreparationView = Field(description="原始不可变受理结果")
    replacement_id: UUID | None = Field(default=None, description="当前替代比较或成功迁移身份")
    superseded_reason: str | None = Field(default=None, description="当前失效原因，原响应不改写")
    current_status: Literal["ready", "conflicted", "superseded"] = Field(description="当前记录状态")
