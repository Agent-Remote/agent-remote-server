"""
按所有者保存和查询原 prune 受理，所有明细与关联由外层保存点整体提交。
"""

from collections.abc import Iterable
from itertools import batched
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_prune_operations import (
    SkillPruneOperation,
    SkillPruneOperationDeletion,
    SkillPruneOperationEntry,
)


class SkillPruneOperationRepository:
    """
    回执查询不加载保留图、原输入树或私有文件。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保留调用方最终提交的事务。

        :param session (AsyncSession): 同一异步事务
        """
        self._session = session

    async def operation(self, user_id: UUID, key: str) -> SkillPruneOperation | None:
        """
        先按所有者和原键查回执，签名轮换不改变已受理事实。

        :param user_id (UUID): 活跃用户身份
        :param key (str): 原始命令键
        :return SkillPruneOperation | None: 不可变原记录或空值
        """
        return await self._session.scalar(
            select(SkillPruneOperation).where(
                SkillPruneOperation.user_id == user_id, SkillPruneOperation.idempotency_key == key
            )
        )

    async def operation_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillPruneOperation | None:
        """
        已知操作身份仍须验证其原始所有者。

        :param user_id (UUID): 活跃用户身份
        :param operation_id (UUID): 原始操作身份
        :return SkillPruneOperation | None: 原记录或空值
        """
        return await self._session.scalar(
            select(SkillPruneOperation).where(
                SkillPruneOperation.user_id == user_id, SkillPruneOperation.id == operation_id
            )
        )

    async def save(
        self,
        operation: SkillPruneOperation,
        entries: Iterable[dict[str, object]],
        deletion_ids: tuple[UUID, ...],
    ) -> None:
        """
        分块写入全部明细控制内存，但绝不分批提交或跳过失败记录。

        :param operation (SkillPruneOperation): 与本次清理共同提交的原受理
        :param entries (Iterable[dict[str, object]]): 原完整披露顺序
        :param deletion_ids (tuple[UUID, ...]): 同事务实际新建任务
        """
        self._session.add(operation)
        await self._session.flush()
        for batch in batched(enumerate(entries), 100, strict=False):
            self._session.add_all(
                [
                    SkillPruneOperationEntry(
                        user_id=operation.user_id,
                        operation_id=operation.id,
                        ordinal=ordinal,
                        disclosure_json=value,
                    )
                    for ordinal, value in batch
                ]
            )
            await self._session.flush()
        for batch_ids in batched(deletion_ids, 100, strict=False):
            self._session.add_all(
                [
                    SkillPruneOperationDeletion(
                        user_id=operation.user_id, operation_id=operation.id, deletion_id=identity
                    )
                    for identity in batch_ids
                ]
            )
            await self._session.flush()

    async def entries(
        self,
        user_id: UUID,
        operation_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        """
        原序号范围查询有界且不重新解释当前业务状态。

        :param user_id (UUID): 当前所有者
        :param operation_id (UUID): 原始操作身份
        :param offset (int): 首条原序号
        :param limit (int): 有界页大小
        :return tuple[dict[str, object], ...]: 原持久化连续披露
        """
        rows = await self._session.scalars(
            select(SkillPruneOperationEntry.disclosure_json)
            .where(
                SkillPruneOperationEntry.user_id == user_id,
                SkillPruneOperationEntry.operation_id == operation_id,
                SkillPruneOperationEntry.ordinal >= offset,
            )
            .order_by(SkillPruneOperationEntry.ordinal)
            .limit(limit)
        )
        return tuple(rows)

    async def progress(self, user_id: UUID, operation_id: UUID) -> tuple[int, int, int, int, int]:
        """
        同一查询汇总原关联任务，任务跨完成状态时不会重复计量。

        :param user_id (UUID): 当前所有者
        :param operation_id (UUID): 原始操作身份
        :return tuple[int, int, int, int, int]: 待完成数、完成数、待删字节、已删字节和重试数
        """
        task = SkillContentDeletion
        link = SkillPruneOperationDeletion
        pending = task.status == "pending"
        complete = task.status == "complete"
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(case((pending, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((complete, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((pending, task.size), else_=0)), 0),
                    func.coalesce(func.sum(case((complete, task.size), else_=0)), 0),
                    func.coalesce(func.sum(case((pending & (task.attempts > 0), 1), else_=0)), 0),
                )
                .select_from(link)
                .join(task, (task.user_id == link.user_id) & (task.id == link.deletion_id))
                .where(
                    link.user_id == user_id,
                    link.operation_id == operation_id,
                )
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4])
