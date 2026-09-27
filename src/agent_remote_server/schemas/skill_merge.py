"""
定义原子目录合并结果和冲突描述。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


class SkillMergeConflict(BaseModel):
    """
    需要显式解决的路径或整项技能冲突。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="冲突相对路径，点表示完整技能")
    reason: Literal["changed_both", "opaque_divergence", "invalid_tree", "source_conflict"] = Field(
        description="无法自动合并的原因"
    )
    unit: tuple[str, ...] = Field(
        default=(), description="目录合并中共同解决的成员，点表示根级辅助范围"
    )


class SkillMergeResult(BaseModel):
    """
    有冲突时不暴露可发布部分树的合并结果。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merged: SkillTreeManifest | None = Field(default=None, description="完整可发布清单")
    conflicts: tuple[SkillMergeConflict, ...] = Field(default=(), description="待解决冲突")


class SkillDirectoryMergeResult(SkillMergeResult):
    """
    完整账户目录的原子结果与跨三份输入固定的连通单元。
    """

    units: tuple[tuple[str, ...], ...] = Field(description="按规范名称排序的独立合并单元")
