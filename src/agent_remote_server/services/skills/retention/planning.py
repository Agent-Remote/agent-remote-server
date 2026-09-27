"""
把精确历史选择展开为完整恢复损失计划，保护、等待和不可退役消费者都显式阻断。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeIs
from uuid import UUID

from agent_remote_server.models.skill_library import SkillRevision
from agent_remote_server.models.skill_local import AccountLocalSkillRevision
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import HistoryRecord, history_records
from agent_remote_server.services.skills.retention.dependencies import history_dependencies
from agent_remote_server.services.skills.retention.history import HistoryRetention
from agent_remote_server.services.skills.retention.retire import RetirableHistory
from agent_remote_server.skill_manager.retention.dependencies import (
    MAX_RETIREMENT_IDENTITIES,
    HistoryDependency,
    dependent_closure,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.retention.projection import RetentionProjection

type RetirementBlocker = Literal[
    "protected",
    "waiting",
    "unknown_release",
    "original_version",
    "account_scope",
    "active_upload",
    "replacement_reference",
]


@dataclass(frozen=True)
class RetirementEntry:
    """
    一项原始或连带恢复损失及全部阻断；虚拟引用没有持久化历史时钟。
    """

    key: RetentionKey
    retained: bool
    content_digests: tuple[str, ...]
    retention: HistoryRetention | None
    blockers: tuple[RetirementBlocker, ...]


@dataclass(frozen=True)
class HistoryRetirementPlan:
    """
    整份计划必须审阅和重验，不能把无阻断的片段当成已授权退役批次。
    """

    user_id: UUID
    account_id: UUID
    requested: tuple[RetentionKey, ...]
    all_unreferenced: bool
    entries: tuple[RetirementEntry, ...]
    dependencies: tuple[HistoryDependency, ...]
    expiring_branches: tuple[tuple[UUID, UUID, int], ...]

    @property
    def ready(self) -> bool:
        """
        只有整个闭包都可退役才允许提交，不承诺任何物理空间释放。

        :return bool: 是否不存在保护、等待或范围阻断
        """
        return all(not row.blockers for row in self.entries)


def state_history(record: HistoryRecord) -> TypeIs[RetirableHistory]:
    """
    包和本地初始版本永远不是 state prune 的隐式退役目标。

    :param record (HistoryRecord): 原始历史记录
    :return TypeIs[RetirableHistory]: 是否属于状态历史范围
    """
    return isinstance(
        record,
        (
            SkillCheckpoint,
            SessionSkillSnapshot,
            SkillFinalization,
            SkillPublication,
            SkillBranchPreparation,
        ),
    )


def retained_history(record: HistoryRecord) -> bool:
    """
    区分可恢复内容与仍存在的审计身份。

    :param record (HistoryRecord): 原始历史记录
    :return bool: 是否仍承诺保留内容
    """
    if isinstance(record, (SkillCheckpoint, SkillRevision, AccountLocalSkillRevision)):
        return record.retained
    return record.content_retired_at is None


def retirement_plan(
    index: RetentionIndex,
    account_id: UUID,
    requested: tuple[RetentionKey, ...],
    histories: tuple[HistoryRetention, ...],
    active_migrations: frozenset[UUID],
    now: datetime,
    *,
    all_unreferenced: bool = False,
    projection: RetentionProjection | None = None,
) -> HistoryRetirementPlan:
    """
    先验证精确初始范围，再反向展开全部保留消费者；预计整理不能忽略旧比较或新成员。

    :param index (RetentionIndex): 同一用户锁内的完整索引
    :param account_id (UUID): 明确账户范围
    :param requested (tuple[RetentionKey, ...]): 原始精确身份选择
    :param histories (tuple[HistoryRetention, ...]): 同时间的真实或整理后预计保护与等待
    :param active_migrations (frozenset[UUID]): 仍有有效输入上传的比较
    :param now (datetime): 本次分析固定时间
    :param all_unreferenced (bool): 是否明确提前结束等待，不能越过依赖或保护
    :param projection (RetentionProjection | None): 已重验整理计划的只读替代内容边
    :return HistoryRetirementPlan: 全部恢复损失、依赖及阻断，不是公共请求回执
    """
    if now.tzinfo is None:
        raise ValueError("retirement planning requires an aware timestamp")
    if (
        not requested
        or len(requested) > MAX_RETIREMENT_IDENTITIES
        or len(set(requested)) != len(requested)
    ):
        raise SkillContentError(
            "INVALID_REQUEST", "select distinct history identities within the retention index limit"
        )
    records = history_records(index)
    for key in requested:
        record = records.get(key)
        if record is None or not state_history(record) or record.account_id != account_id:
            raise SkillContentError("HISTORY_NOT_FOUND", "history not found in this account")
    virtual = (
        {RetentionKey("checkpoint", str(row.id)) for row in projection.checkpoints}
        if projection is not None
        else set()
    )
    retained = frozenset({key for key, row in records.items() if retained_history(row)} | virtual)
    closure = dependent_closure(requested, history_dependencies(index, projection), retained)
    views = {row.key: row for row in histories}
    entries = []
    for key in closure.required:
        if key in virtual:
            entries.append(RetirementEntry(key, True, (), None, ("replacement_reference",)))
            continue
        record = records[key]
        view = views[key]
        blockers: list[RetirementBlocker] = []
        if key in retained:
            if not state_history(record):
                blockers.append("original_version")
            elif record.account_id != account_id:
                blockers.append("account_scope")
            if view.reasons:
                blockers.append("protected")
            if key.kind == "migration" and record.id in active_migrations:
                blockers.append("active_upload")
            if not all_unreferenced:
                if view.released_at is None:
                    blockers.append("unknown_release")
                elif view.expires_at is None or view.expires_at > now:
                    blockers.append("waiting")
        entries.append(
            RetirementEntry(key, key in retained, _digests(record), view, tuple(blockers))
        )
    required = set(closure.required)
    replaced_branches = set(dict(projection.branch_heads)) if projection is not None else set()
    return HistoryRetirementPlan(
        user_id=index.user_id,
        account_id=account_id,
        requested=tuple(sorted(requested)),
        all_unreferenced=all_unreferenced,
        entries=tuple(entries),
        dependencies=closure.dependencies,
        expiring_branches=tuple(
            sorted(
                (row.id, row.head_checkpoint_id, row.epoch)
                for row in index.branches
                if not row.expired
                and row.head_checkpoint_id is not None
                and RetentionKey("checkpoint", str(row.head_checkpoint_id)) in required
                and RetentionKey("checkpoint", str(row.head_checkpoint_id)) in retained
                and row.id not in replaced_branches
            )
        ),
    )


def _digests(record: HistoryRecord) -> tuple[str, ...]:
    """
    绑定原始内容证据，退役计划重验不能接受同身份被替换的比较或树。

    :param record (HistoryRecord): 原始历史记录
    :return tuple[str, ...]: 审计内容摘要，已退役可空引用不会重新授予读取权
    """
    values: tuple[str | None, ...]
    if isinstance(record, SkillCheckpoint):
        values = (record.content_digest,)
    elif isinstance(record, SkillPublication):
        values = (record.current_tree_digest,)
    elif isinstance(record, SkillBranchPreparation):
        values = (record.base_digest, record.current_digest, record.incoming_digest)
    else:
        values = (record.tree_digest,)
    return tuple(value for value in values if value is not None)
