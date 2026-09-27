"""
定义上传前的纯清单预览，不把候选元数据完整性表示为发布授权。
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff
from agent_remote_server.skill_manager.manifest import manifest_digest


class SkillResolutionPreviewRequest(BaseModel):
    """
    人工清单只在本次计算中使用，摘要不能成为持久内容授权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 2, description="预期解决计划版本")
    choice: SkillResolutionChoice = Field(description="一个人工文件或目录选择")
    manifest: SkillTreeManifest = Field(description="本机已固定内容的完整清单，不含文件字节")

    @model_validator(mode="after")
    def match_content(self) -> Self:
        """
        清单与所选摘要必须完全对应，不能混用已有侧选择和人工内容。

        :return Self: 人工内容身份明确的预览
        """
        if self.choice.tree_digest != manifest_digest(self.manifest):
            raise ValueError("preview manifest must match one custom content choice")
        return self


class SkillResolutionContentPreview(BaseModel):
    """
    固定输入、差异和待处理冲突可解释，但没有文件验证或任何已提交结果。
    """

    kind: Literal["publication", "migration"] = Field(description="原冲突身份域")
    conflict_id: UUID = Field(description="原始冲突身份")
    account_id: UUID = Field(description="原始账户范围")
    plan_revision: int = Field(ge=0, description="此次读取的计划版本，不做递增")
    proposed_tree_digest: str = Field(description="本次人工清单摘要")
    choices: list[SkillResolutionChoice] = Field(description="替换重叠范围后的完整拟议选择")
    metadata_only: Literal[True] = Field(default=True, description="仅计算清单元数据")
    content_verified: Literal[False] = Field(default=False, description="尚未校验本次人工文件字节")
    ready_to_publish: Literal[False] = Field(default=False, description="预览绝不提供发布授权")
    candidate_complete: bool = Field(description="清单层面是否覆盖全部冲突且结构完整")
    result_tree_digest: str | None = Field(description="完整候选目录摘要，不完整时为空")
    remaining: tuple[SkillMergeConflict, ...] = Field(description="仍需明确选择或结构修复的冲突")
    unit: tuple[str, ...] = Field(description="迁移关联单元，会话完整目录为空")
    current_tree_digest: str = Field(description="原比较当前侧摘要")
    directory_tree_digest: str = Field(description="比较所依据的保存完整目录摘要")
    changes: list[SkillStatePathDiff] | None = Field(description="相对保存目录的完整拟议变化")
    target_revision_id: UUID | None = Field(description="迁移目标原始版本，会话冲突为空")
    target_modified: bool | None = Field(description="迁移候选相对目标原始包的元数据修改标记")
    target_changes: list[SkillStatePathDiff] | None = Field(
        description="迁移相对目标自身保存当前侧的全部变化"
    )
    original_changes: list[SkillStatePathDiff] | None = Field(
        description="迁移相对目标原始包的全部覆盖"
    )
    pending_checks: tuple[str, ...] = Field(
        default=("custom_content", "source_authorization", "quota_admission", "head_preconditions"),
        description="实际提交前仍须通过的内容和发布检查",
    )
