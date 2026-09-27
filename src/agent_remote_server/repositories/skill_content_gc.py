"""
持有用户锁时读写精确内容引用和删除任务，所有 SQL 留在仓储层。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillStoredTree,
    SkillTreeObjectReference,
)


class SkillContentGCRepository:
    """
    提供分块完整对象读取及独立 worker 的已提交任务定位。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方事务。

        :param session (AsyncSession): 已授权的异步事务
        """
        self.session = session

    async def objects(self, user_id: UUID, digests: set[str]) -> tuple[SkillContentObject, ...]:
        """
        同时读取两个分类，不把 SQL 分块变成部分成功。

        :param user_id (UUID): 已锁定用户
        :param digests (set[str]): 本次精确共享摘要
        :return tuple[SkillContentObject, ...]: 全部匹配的最新对象行
        """
        if len(digests) > 1_000_000:
            raise ValueError("content reclamation identity limit exceeded")
        ordered = sorted(digests)
        result: list[SkillContentObject] = []
        for start in range(0, len(ordered), 500):
            result.extend(
                await self.session.scalars(
                    select(SkillContentObject)
                    .where(
                        SkillContentObject.user_id == user_id,
                        SkillContentObject.digest.in_(ordered[start : start + 500]),
                    )
                    .execution_options(populate_existing=True)
                )
            )
        return tuple(result)

    async def remove_tree(self, tree: SkillStoredTree) -> None:
        """
        先删指定树的对象边，再删树；任何 retained 历史外键仍由数据库阻断。

        :param tree (SkillStoredTree): 已重验授权的精确树
        """
        await self.session.execute(
            delete(SkillTreeObjectReference).where(
                SkillTreeObjectReference.user_id == tree.user_id,
                SkillTreeObjectReference.category == tree.category,
                SkillTreeObjectReference.tree_digest == tree.digest,
            )
        )
        await self.session.delete(tree)

    async def task(self, identity: UUID) -> SkillContentDeletion | None:
        """
        worker 先定位所有者，取得用户锁后必须重新读取同一任务。

        :param identity (UUID): 持久化任务原始 UUID
        :return SkillContentDeletion | None: 刷新后的任务或不存在
        """
        return await self.session.scalar(
            select(SkillContentDeletion)
            .where(SkillContentDeletion.id == identity)
            .execution_options(populate_existing=True)
        )

    async def due(self, now: datetime, limit: int) -> tuple[UUID, ...]:
        """
        扫描只是可重复投递；执行阶段仍在用户锁内重验，不提前租出可越锁删除的权限。

        :param now (datetime): 固定扫描时间
        :param limit (int): 单轮最大任务数
        :return tuple[UUID, ...]: 到期任务原 UUID
        """
        if not 1 <= limit <= 1000:
            raise ValueError("content deletion batch limit is out of range")
        return tuple(
            await self.session.scalars(
                select(SkillContentDeletion.id)
                .where(
                    SkillContentDeletion.status == "pending",
                    SkillContentDeletion.next_attempt_at <= now,
                )
                .order_by(SkillContentDeletion.next_attempt_at, SkillContentDeletion.id)
                .limit(limit)
            )
        )

    def add_task(self, task: SkillContentDeletion) -> None:
        """
        删除标记与任务由调用方同一保存点提交。

        :param task (SkillContentDeletion): 尚未提交的原任务
        """
        self.session.add(task)

    async def remove_object(self, obj: SkillContentObject) -> None:
        """
        只删除已经重验没有本分类树边和有效租约的精确对象。

        :param obj (SkillContentObject): 已锁定对象
        """
        await self.session.delete(obj)

    async def flush(self) -> None:
        """
        让外键和配额约束在业务保存点内生效，不替调用方提交。
        """
        await self.session.flush()
