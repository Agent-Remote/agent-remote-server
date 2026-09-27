"""
定义完整恢复损失、依赖和等价整理的有界分页行，不传输文件正文。
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.skill_manager.retention.graph import RetentionKind


class PruneIdentity(BaseModel):
    """
    明确历史种类和稳定身份，不依赖名称解释恢复损失。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: RetentionKind = Field(description="历史种类")
    id: UUID = Field(description="原始稳定身份")


class PruneHistoryRow(BaseModel):
    """
    一项完整候选或连带历史的资格和最终是否选中。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["history"] = Field(default="history", description="历史披露行")
    history: PruneIdentity = Field(description="原始历史身份")
    retained: bool = Field(description="本次预览时是否仍可恢复")
    selected: bool = Field(description="是否确认退役这项内容")
    group: int | None = Field(ge=1, description="不可拆分损失组号，未选中为空")
    blockers: tuple[str, ...] = Field(description="直接阻断原因")
    dependency_blocked: bool = Field(description="是否被直接或连带消费者阻断")
    protected_by: tuple[str, ...] = Field(description="所有硬保护理由")
    released_at: datetime | None = Field(description="原始或预计最后解除保护时间")
    expires_at: datetime | None = Field(description="原始或预计保留截止")
    archived: bool = Field(description="是否适用卸载归档等待")
    content_digests: tuple[str, ...] = Field(description="原始内容摘要证据，非独立读取授权")


class PruneDependencyRow(BaseModel):
    """
    完整消费者对输入的保留义务，所有依赖均可遍历。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["dependency"] = Field(default="dependency", description="依赖披露行")
    consumer: PruneIdentity = Field(description="保留消费者")
    dependency: PruneIdentity = Field(description="被承诺恢复的输入")
    relation: str = Field(description="依赖字段或成员关系")


class PruneCompactionRow(BaseModel):
    """
    一项等价 head 替换；预览无真实结果身份，受理详情补齐。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["compaction"] = Field(default="compaction", description="等价整理行")
    scope: Literal["item", "directory"] = Field(description="被替换视图范围")
    checkpoint_id: UUID = Field(description="原始检查点")
    state_id: UUID | None = Field(description="单项分支身份，完整目录为空")
    epoch: int | None = Field(description="原纪元，整理不推进纪元")
    original_digest: str = Field(description="原完整 backing 树")
    result_digest: str = Field(description="等价整理后的完整 backing 树")
    replacement_id: UUID | None = Field(default=None, description="受理后实际创建的结果身份")


class PruneMemberRow(BaseModel):
    """
    精确成员根的移除或链接阻断，不把同名其他版本当作同一选择。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["member"] = Field(default="member", description="目录成员披露行")
    directory_id: UUID = Field(description="原完整目录检查点")
    checkpoint_id: UUID = Field(description="原精确单项检查点")
    state_id: UUID = Field(description="原精确运行分支")
    name: str = Field(description="成员发现根名称")
    action: Literal["removed", "blocked"] = Field(description="整理移除或关联阻断")


type PruneDisclosure = Annotated[
    PruneHistoryRow | PruneDependencyRow | PruneCompactionRow | PruneMemberRow,
    Field(discriminator="kind"),
]
