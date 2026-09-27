"""
在显式业务保存点中记录最后解除保护的时间，不从扫描推断旧释放事件。
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_library import SkillRevision
from agent_remote_server.models.skill_local import AccountLocalSkillRevision
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStoredTree
from agent_remote_server.repositories.skill_retention import (
    RetentionIndex,
    SkillRetentionRepository,
)
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionKind

type HistoryRecord = (
    SkillRevision
    | AccountLocalSkillRevision
    | SkillCheckpoint
    | SessionSkillSnapshot
    | SkillFinalization
    | SkillPublication
    | SkillBranchPreparation
)

_ACTIVE: ContextVar[frozenset[tuple[AsyncSession, UUID]]] = ContextVar(
    "skill_retention_mutations", default=frozenset()
)


def history_records(index: RetentionIndex) -> dict[RetentionKey, HistoryRecord]:
    """
    将真实历史身份映射到保活图，合成目录上下文与增量基线不另计完整比较保留期。

    :param index (RetentionIndex): 同一用户的完整引用索引
    :return dict[RetentionKey, HistoryRecord]: 含持久化释放时钟的真实历史记录
    """
    groups: tuple[tuple[RetentionKind, Sequence[HistoryRecord]], ...] = (
        ("revision", index.revisions),
        ("local_revision", index.local_revisions),
        ("checkpoint", index.checkpoints),
        ("snapshot", index.snapshots),
        ("finalization", index.finalizations),
        ("publication", index.publications),
        ("migration", index.migrations),
    )
    return {RetentionKey(kind, str(row.id)): row for kind, rows in groups for row in rows}


def tree_records(index: RetentionIndex) -> dict[RetentionKey, SkillStoredTree]:
    """
    保留分类独立的完整树时钟，不把摘要身份混入 UUID 历史选择。

    :param index (RetentionIndex): 同一用户完整引用索引
    :return dict[RetentionKey, SkillStoredTree]: 按分类和摘要标识的真实完整树
    """
    return {
        RetentionKey("package_tree" if row.category == "package" else "state_tree", row.digest): row
        for row in index.trees
    }


def clock_records(index: RetentionIndex) -> dict[RetentionKey, HistoryRecord | SkillStoredTree]:
    """
    合并本次需要记录释放事件的身份，历史查询与退役范围仍由原模型单独决定。

    :param index (RetentionIndex): 同一事务完整索引
    :return dict[RetentionKey, HistoryRecord | SkillStoredTree]: 真实历史与完整树的等待时钟
    """
    return {**history_records(index), **tree_records(index)}


@asynccontextmanager
async def retention_mutation(
    session: AsyncSession, user_id: UUID, *, read_only: bool = False
) -> AsyncIterator[None]:
    """
    引用与时钟共用用户锁和保存点，嵌套调用只由最外层观察最终可见变化。

    :param session (AsyncSession): 调用方持有并最终提交的异步事务
    :param user_id (UUID): 已认证内容所有者
    :param read_only (bool): 预览沿用保存点但不分析或写入时钟
    :return AsyncIterator[None]: 由调用方执行真实业务变更的上下文
    """
    identity = (session, user_id)
    outermost = not read_only and identity not in _ACTIVE.get()
    storage = SkillStorageRepository(session)
    locked = False
    if outermost:
        locked = await storage.lock_existing_usage_for_mutation(user_id)
    async with session.begin_nested():
        if not outermost:
            yield
            return
        token = _ACTIVE.set(_ACTIVE.get() | {identity})
        try:
            if not locked:
                await storage.lock_usage(user_id)
            repository = SkillRetentionRepository(session)
            before = await repository.load(user_id)
            known = frozenset(clock_records(before))
            protected_before = frozenset(protection(before, datetime.now(UTC)).protected)
            del before
            yield
            await session.flush()
            after = await repository.load(user_id)
            released_at = datetime.now(UTC)
            protected_after = protection(after, released_at).protected
            for key, record in clock_records(after).items():
                if key in protected_after:
                    record.retention_released_at = None
                elif key in protected_before or key not in known:
                    record.retention_released_at = released_at
            await session.flush()
        finally:
            _ACTIVE.reset(token)
