"""
提供内部完整历史计划预览与同保存点重验执行，不提供公共请求幂等或物理删除。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.services.skills.retention.planning import (
    HistoryRetirementPlan,
    retirement_plan,
)
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillHistoryRetirementPlanner:
    """
    全量消费者闭包必须先审阅，实际写入仍复用统一退役校验与用户锁。
    """

    def __init__(self, session: AsyncSession, policy: SkillStoragePolicy) -> None:
        """
        复用已授权调用方事务和同一保留策略。

        :param session (AsyncSession): 外层最终提交的事务
        :param policy (SkillStoragePolicy): 当前部署等待配置
        """
        self._session = session
        self._policy = policy
        self._storage = SkillStorageRepository(session)
        self._repository = SkillRetentionRepository(session)

    async def preview(
        self,
        user_id: UUID,
        account_id: UUID,
        keys: tuple[RetentionKey, ...],
        *,
        all_unreferenced: bool = False,
    ) -> HistoryRetirementPlan:
        """
        只锁已有用户，完整预览不登记上传、操作、虚拟历史或时钟。

        :param user_id (UUID): 当前已认证所有者
        :param account_id (UUID): 精确账户范围
        :param keys (tuple[RetentionKey, ...]): 待分析初始历史身份
        :param all_unreferenced (bool): 是否明确提前结束等待
        :return HistoryRetirementPlan: 完整恢复损失与阻断
        """
        if await self._storage.lock_existing_usage(user_id) is None:
            raise SkillContentError("HISTORY_NOT_FOUND", "history not found in this account")
        index = await self._repository.load(user_id)
        now = datetime.now(UTC)
        active = await self._repository.active_migration_ids(
            user_id, tuple(row.id for row in index.migrations), now
        )
        return retirement_plan(
            index,
            account_id,
            keys,
            history_retention(index, protection(index, now), self._policy),
            active,
            now,
            all_unreferenced=all_unreferenced,
        )

    async def apply(
        self, user_id: UUID, expected: HistoryRetirementPlan
    ) -> tuple[RetentionKey, ...]:
        """
        重建完整闭包并精确比较后同时退役，任何新消费者、保护或等待变化都使原计划失效。

        :param user_id (UUID): 当前已认证所有者
        :param expected (HistoryRetirementPlan): 已审阅的全部恢复损失计划
        :return tuple[RetentionKey, ...]: 实际退役身份，不是配额释放或公共幂等回执
        """
        if expected.user_id != user_id:
            raise SkillContentError("HISTORY_NOT_FOUND", "history not found in this account")
        async with retention_mutation(self._session, user_id):
            actual = await self.preview(
                user_id,
                expected.account_id,
                expected.requested,
                all_unreferenced=expected.all_unreferenced,
            )
            if actual != expected:
                raise SkillContentError("HEAD_CHANGED", "history retirement preview has changed")
            if not actual.ready:
                raise SkillContentError(
                    "HISTORY_REFERENCED", "history retirement plan has blockers"
                )
            return await SkillHistoryRetirementService(self._session, self._policy).retire(
                user_id,
                actual.account_id,
                tuple(row.key for row in actual.entries),
                all_unreferenced=actual.all_unreferenced,
            )
