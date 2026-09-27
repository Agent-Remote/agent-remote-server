"""
独立事务消费已提交的物理删除任务，持久化标记和用户锁覆盖真实磁盘操作。
"""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_storage import SkillContentObject
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.gc.planning import upload_leases, utc
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

type DeletionOutcome = Literal["missing", "not_due", "pending", "complete"]


class SkillContentDeletionWorker:
    """
    使用原任务 UUID 重验，绝不接收外层尚未提交的业务事务作为删除权限。
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], store: PrivateObjectStore
    ) -> None:
        """
        worker 只持有独立事务工厂和私有字节层。

        :param sessions (async_sessionmaker[AsyncSession]): 新建独立事务的工厂
        :param store (PrivateObjectStore): 用户私有不可变对象存储
        """
        self._sessions = sessions
        self._store = store

    async def run_once(self, limit: int = 100) -> tuple[tuple[UUID, DeletionOutcome], ...]:
        """
        有界扫描已提交任务；多 worker 可重复读取，实际执行仍由用户锁和原任务身份串行。

        :param limit (int): 本轮最大任务数
        :return tuple[tuple[UUID, DeletionOutcome], ...]: 每个原任务的处理状态
        """
        async with self._sessions() as session:
            identities = await SkillContentGCRepository(session).due(datetime.now(UTC), limit)
        results: list[tuple[UUID, DeletionOutcome]] = []
        for identity in identities:
            results.append((identity, await self.process(identity)))
        return tuple(results)

    async def process(self, identity: UUID) -> DeletionOutcome:
        """
        原标记必须已提交，整个重验、磁盘删除和终态提交期间持有用户锁。

        :param identity (UUID): 可重复投递的原任务 UUID
        :return DeletionOutcome: 当前处理结果，错误时保留 pending 及重试时间
        """
        async with self._sessions.begin() as session:
            repository = SkillContentGCRepository(session)
            task = await repository.task(identity)
            if task is None:
                return "missing"
            if not await SkillStorageRepository(session).lock_existing_usage_for_mutation(
                task.user_id
            ):
                return "pending"
            task = await repository.task(identity)
            if task is None:
                return "missing"
            if task.status == "complete":
                return "complete"
            now = datetime.now(UTC)
            if utc(task.next_attempt_at) > now:
                return "not_due"
            task.attempts += 1
            rows = await repository.objects(task.user_id, {task.digest})
            error = await self._preflight(session, task, rows, now)
            if error is None:
                try:
                    await self._store.delete_committed(
                        task.user_id, task.digest, task.size, task.id
                    )
                except (OSError, ValueError):
                    error = "content_io_error"
            if error is not None:
                task.last_error_code = error
                task.next_attempt_at = now + timedelta(
                    seconds=min(3600, 2 ** min(task.attempts, 12))
                )
                await repository.flush()
                return "pending"
            for row in rows:
                await repository.remove_object(row)
            task.status = "complete"
            task.completed_at = datetime.now(UTC)
            task.last_error_code = None
            await repository.flush()
            return "complete"

    async def _preflight(
        self,
        session: AsyncSession,
        task: SkillContentDeletion,
        rows: tuple[SkillContentObject, ...],
        now: datetime,
    ) -> str | None:
        """
        精确标记、全部树边和所有类别有效租约必须与实际删除兼容。

        :param session (AsyncSession): 独立且锁定用户的事务
        :param task (SkillContentDeletion): 仍 pending 的原始任务
        :param rows (tuple[SkillContentObject, ...]): 同摘要全部分类对象
        :param now (datetime): 固定重验时刻
        :return str | None: 固定错误码，空表示可执行磁盘删除
        """
        mask = sum(1 if row.category == "package" else 2 for row in rows)
        if mask != task.category_mask or any(
            row.status != "deleting" or row.size != task.size for row in rows
        ):
            return "deletion_marker_changed"
        try:
            index = await SkillRetentionRepository(session).load(task.user_id)
        except ValueError:
            return "content_index_invalid"
        if any(ref.object_digest == task.digest for ref in index.tree_objects):
            return "content_referenced"
        if upload_leases(index, {task.digest}, now).get(task.digest):
            return "content_upload_active"
        return None
