"""
在一致用户事务中解释保活根和历史引用，不执行退役或物理删除。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.history import (
    HistoryRetention,
    history_retention,
)
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention, tree_retention
from agent_remote_server.skill_manager.retention.graph import RetentionGraph, RetentionProtection
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillRetentionInspector:
    """
    内部只读分析入口，预览不能启动或推进回收时钟。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用内容与配置写入的同一用户锁顺序。

        :param session (AsyncSession): 请求外层事务
        """
        self._storage = SkillStorageRepository(session)
        self._repository = SkillRetentionRepository(session)

    async def inspect(self, user_id: UUID) -> RetentionProtection:
        """
        新用户不创建计量记录，已有用户取得一致引用视图。

        :param user_id (UUID): 已认证所有者
        :return RetentionProtection: 保护理由和目录整理义务，不是删除授权
        """
        if await self._storage.lock_existing_usage(user_id) is None:
            return RetentionGraph().protect()
        index = await self._repository.load(user_id)
        return protection(index, datetime.now(UTC))

    async def history(
        self, user_id: UUID, policy: SkillStoragePolicy
    ) -> tuple[HistoryRetention, ...]:
        """
        在一致只读视图中解释等待截止时间，不把到期等同于可直接删除。

        :param user_id (UUID): 已认证所有者
        :param policy (SkillStoragePolicy): 当前部署保留策略
        :return tuple[HistoryRetention, ...]: 保护理由、归档分类和可证明的等待截止
        """
        if await self._storage.lock_existing_usage(user_id) is None:
            return ()
        index = await self._repository.load(user_id)
        return history_retention(index, protection(index, datetime.now(UTC)), policy)

    async def trees(
        self, user_id: UUID, policy: SkillStoragePolicy
    ) -> tuple[StoredTreeRetention, ...]:
        """
        列出真实完整树等待与全部历史外键，不为预览创建状态或启动回收。

        :param user_id (UUID): 已认证所有者
        :param policy (SkillStoragePolicy): 当前部署保留配置
        :return tuple[StoredTreeRetention, ...]: 分类树的保护、截止和实际引用
        """
        if await self._storage.lock_existing_usage(user_id) is None:
            return ()
        index = await self._repository.load(user_id)
        return tree_retention(index, protection(index, datetime.now(UTC)), policy)
