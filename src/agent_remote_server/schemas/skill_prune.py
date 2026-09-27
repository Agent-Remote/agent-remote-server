"""
定义紧凑 prune 确认、不可变受理和独立删除进度，完整损失通过有界分页展示。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_prune_rows import PruneDisclosure
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector


class PrunePreviewRequest(BaseModel):
    """
    首次范围与后续签名游标，不允许调用方指定用户或临时替换截止。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: SkillStateSelector = Field(description="首次名称或后续规范稳定来源范围")
    all_unreferenced: bool = Field(default=False, strict=True, description="是否明确提前结束等待")
    cursor: str | None = Field(default=None, max_length=4096, description="上页原始签名游标")
    limit: int = Field(default=100, ge=1, le=100, strict=True, description="单页完整披露行数")


class PruneBinding(BaseModel):
    """
    每页和回执共同绑定的原规范范围、时间与完整计划摘要。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: SkillStateSelector = Field(description="使用稳定身份的原规范范围")
    cutoff: datetime = Field(description="原始固定分析截止")
    all_unreferenced: bool = Field(strict=True, description="原等待模式")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="完整内部计划的规范摘要")


class PruneSummary(BaseModel):
    """
    与完整披露对应的动作和预计结算，不能把待删字节表述成磁盘已释放。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    binding: PruneBinding = Field(description="原确认身份")
    ready: bool = Field(description="整份动作是否可以执行")
    history_losses: int = Field(ge=0, description="将失去恢复能力的真实历史数")
    groups: int = Field(ge=0, description="完整损失依赖组数")
    blocked_histories: int = Field(ge=0, description="直接或连带阻断的历史数")
    compacted_directories: int = Field(ge=0, description="需创建等价结果的目录数")
    compacted_items: int = Field(ge=0, description="需创建等价结果的单项头数")
    trees: int = Field(ge=0, description="本次确实可删除的完整状态树数")
    package_bytes: int = Field(ge=0, description="原始包逻辑释放字节")
    state_bytes: int = Field(ge=0, description="状态分类逻辑释放字节")
    pending_file_bytes: int = Field(ge=0, description="预计提交删除任务的物理字节")


class PrunePreviewPage(BaseModel):
    """
    连续披露页，只有完整遍历后的末页包含执行确认凭据。
    """

    summary: PruneSummary = Field(description="所有页相同的原始预计结果")
    offset: int = Field(ge=0, description="本页从零开始的原序号")
    total: int = Field(ge=0, description="整份完整披露条数")
    rows: tuple[PruneDisclosure, ...] = Field(max_length=100, description="连续完整披露行")
    next_cursor: str | None = Field(description="下一页原签名游标")
    confirmation: str | None = Field(description="仅末页返回的原签名执行凭据")


class PruneCommand(BaseModel):
    """
    原请求仅含原键和已完整展示的确认凭据，适合持久化紧凑恢复日志。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="用户原幂等键"
    )
    confirmation: str = Field(min_length=1, max_length=4096, description="原完整预览的末页执行凭据")


class PruneReceipt(BaseModel):
    """
    原子已接受结果永久保持原值，当前删除进度另行读取。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID = Field(description="原始已提交操作身份")
    idempotency_key: str = Field(description="原请求幂等键")
    status: Literal["accepted"] = Field(default="accepted", description="不可变原受理状态")
    confirmation_fingerprint: str = Field(description="原确认凭据的散列，不存储原签名凭据")
    summary: PruneSummary = Field(description="已完整重验并实际执行的原动作与结算")
    disclosure_rows: int = Field(ge=0, description="可独立分页恢复的全部原披露条数")


class PruneReceiptPage(BaseModel):
    """
    原受理披露保持顺序和完整数量，不重新读取已回收内容。
    """

    operation_id: UUID = Field(description="原操作身份")
    offset: int = Field(ge=0, description="本页原序号")
    total: int = Field(ge=0, description="原披露总条数")
    rows: tuple[PruneDisclosure, ...] = Field(max_length=100, description="原持久化披露行")
    next_offset: int | None = Field(description="后续原序号，最后一页为空")


class PruneDeletionProgress(BaseModel):
    """
    只汇总原操作真正关联的任务，原逻辑额度不再次结算。
    """

    operation_id: UUID = Field(description="原操作身份")
    pending_tasks: int = Field(ge=0, description="仍等待完成的原任务数")
    completed_tasks: int = Field(ge=0, description="已经完成的原任务数")
    pending_file_bytes: int = Field(ge=0, description="仍待物理删除的去重字节")
    deleted_file_bytes: int = Field(ge=0, description="原任务已完成物理删除的字节")
    retrying_tasks: int = Field(ge=0, description="已有失败尝试但尚未完成的任务数")
