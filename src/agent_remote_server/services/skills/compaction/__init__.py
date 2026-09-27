"""
提供内部只读整理预览与精确计划原子发布，公开 prune 仍须持久化原请求和删除回执。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.compaction.apply import publish_compaction
from agent_remote_server.services.skills.compaction.build import CompactionBuilder
from agent_remote_server.services.skills.compaction.plan import CompactionPlan, CompactionResult
from agent_remote_server.services.skills.compaction.projection import (
    CompactionRetentionPreview,
    project_retention,
)
from agent_remote_server.services.skills.compaction.reclamation import (
    CompactionReclamationPreview,
    CompactionReclamationResult,
    forecast_reclamation,
    same_reclamation_actions,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.planning import (
    HistoryRetirementPlan,
    retirement_plan,
)
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillDirectoryCompactionService:
    """
    同账户精确选择不包含删除授权，调用方始终保留最终事务提交权。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        复用通常状态查询、内容验证与 user lock，所有 SQL 保持在仓储。

        :param session (AsyncSession): 已认证调用方事务
        :param store (PrivateObjectStore): 私有完整对象卷
        :param policy (SkillStoragePolicy): 当前配额和保留策略
        """
        self._session = session
        self._policy = policy
        self._queries = SkillStateQueryService(session, store, policy)
        self._repository = SkillRetentionRepository(session)
        self._publication = SkillPublicationRepository(session)
        self._builder = CompactionBuilder(MigrationContent(self._queries, store), policy)

    async def preview(
        self,
        user_id: UUID,
        account_id: UUID,
        checkpoint_ids: tuple[UUID, ...],
        *,
        all_unreferenced: bool = False,
    ) -> CompactionPlan:
        """
        对现有用户取一致读锁，新用户或无效选择不创建任何持久化对象。

        :param user_id (UUID): 已认证所有者
        :param account_id (UUID): 明确目标账户
        :param checkpoint_ids (tuple[UUID, ...]): 待移除的精确历史 item 身份
        :param all_unreferenced (bool): 是否明确提前终止等待期
        :return CompactionPlan: 尚未创建上传或新身份的完整计划
        """
        await self._queries.library.read_library(user_id)
        index = await self._repository.load(user_id)
        return await self._builder.build(
            index,
            account_id,
            checkpoint_ids,
            await self._queries.library.generation(user_id),
            all_unreferenced=all_unreferenced,
        )

    async def retention_preview(
        self, user_id: UUID, expected: CompactionPlan, *, analyzed_at: datetime | None = None
    ) -> CompactionRetentionPreview:
        """
        原计划完整重验后推演保活闭包，不修改 ORM、创建虚拟记录或启动持久化时钟。

        :param user_id (UUID): 当前已认证所有者
        :param expected (CompactionPlan): 先前只读预览的完整精确计划
        :param analyzed_at (datetime | None): 重验原预览时固定的分析时间
        :return CompactionRetentionPreview: 整理后的保护与原始历史预计等待
        """
        now = datetime.now(UTC)
        if analyzed_at is not None:
            if analyzed_at.utcoffset() is None or analyzed_at > now:
                raise SkillContentError("INVALID_REQUEST", "invalid compaction analysis time")
            now = analyzed_at
        if expected.user_id != user_id:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "compaction account not found")
        await self._queries.library.read_library(user_id)
        index = await self._repository.load(user_id)
        actual = await self._builder.build(
            index,
            expected.account_id,
            expected.checkpoint_ids,
            await self._queries.library.generation(user_id),
            all_unreferenced=expected.all_unreferenced,
        )
        if actual != expected:
            raise SkillContentError("HEAD_CHANGED", "directory compaction preview has changed")
        return project_retention(index, actual, now, self._policy)

    async def retirement_preview(
        self, user_id: UUID, expected: CompactionPlan, *, analyzed_at: datetime | None = None
    ) -> tuple[CompactionRetentionPreview, HistoryRetirementPlan]:
        """
        从整理后保护和新旧成员依赖展开整份历史损失，不把仍等待的旧等价视图忽略。

        :param user_id (UUID): 已认证所有者
        :param expected (CompactionPlan): 先前审阅的精确整理计划
        :param analyzed_at (datetime | None): 原始分析时间，默认当前时间
        :return tuple[CompactionRetentionPreview, HistoryRetirementPlan]: 完整整理投影和连带退役阻断
        """
        projected = await self.retention_preview(user_id, expected, analyzed_at=analyzed_at)
        index = await self._repository.load(user_id)
        active = await self._repository.active_migration_ids(
            user_id, tuple(row.id for row in index.migrations), projected.analyzed_at
        )
        retirement = retirement_plan(
            index,
            expected.account_id,
            tuple(
                RetentionKey("checkpoint", str(identity)) for identity in expected.checkpoint_ids
            ),
            projected.history,
            active,
            projected.analyzed_at,
            all_unreferenced=expected.all_unreferenced,
            projection=projected.projection,
        )
        return projected, retirement

    async def apply(self, user_id: UUID, expected: CompactionPlan) -> CompactionResult:
        """
        重读原选择并精确比较，任一过期资格或并发变化都不能落下半份新目录。

        :param user_id (UUID): 当前已认证所有者
        :param expected (CompactionPlan): 已审阅的原始完整计划
        :return CompactionResult: 同保存点实际发布身份，外层尚可回滚
        """
        if expected.user_id != user_id:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "compaction account not found")
        async with retention_mutation(self._session, user_id):
            index = await self._repository.load(user_id)
            actual = await self._builder.build(
                index,
                expected.account_id,
                expected.checkpoint_ids,
                await self._queries.library.generation(user_id),
                all_unreferenced=expected.all_unreferenced,
            )
            if actual != expected:
                raise SkillContentError("HEAD_CHANGED", "directory compaction preview has changed")
            return await publish_compaction(
                actual, index, self._queries.runtime, self._publication, self._queries.content
            )

    async def reclamation_preview(
        self, user_id: UUID, expected: CompactionPlan, *, analyzed_at: datetime | None = None
    ) -> CompactionReclamationPreview:
        """
        同锁预测整份历史退役后的真实外键与分类额度，预览提交也不写入任何记录。

        :param user_id (UUID): 已认证所有者
        :param expected (CompactionPlan): 原始精确整理计划
        :param analyzed_at (datetime | None): 重验时沿用原始分析时间
        :return CompactionReclamationPreview: 全部历史损失、阻断和内容结算预测
        """
        projected, retirement = await self.retirement_preview(
            user_id, expected, analyzed_at=analyzed_at
        )
        index = await self._repository.load(user_id)
        return await forecast_reclamation(
            index, projected, retirement, SkillContentGCRepository(self._session), self._policy
        )

    async def apply_reclamation(
        self, user_id: UUID, expected: CompactionReclamationPreview
    ) -> CompactionReclamationResult:
        """
        原预测完整重验后在单个保存点整理、退役和登记回收；任何阶段失败都整体撤销。

        :param user_id (UUID): 已认证所有者
        :param expected (CompactionReclamationPreview): 已审阅的精确原预测，非公开幂等回执
        :return CompactionReclamationResult: 尚待外层提交的完整组合结果
        """
        plan = expected.retention.plan
        if plan.user_id != user_id:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "compaction account not found")
        async with retention_mutation(self._session, user_id):
            actual = await self.reclamation_preview(
                user_id, plan, analyzed_at=expected.retention.analyzed_at
            )
            if actual != expected:
                raise SkillContentError(
                    "HEAD_CHANGED", "compaction reclamation preview has changed"
                )
            if not actual.ready or actual.content is None:
                raise SkillContentError("STATE_PROTECTED", "complete retirement plan is blocked")
            compacted = await self.apply(user_id, plan)
            retired = await SkillHistoryRetirementService(self._session, self._policy).retire(
                user_id,
                plan.account_id,
                tuple(row.key for row in actual.retirement.entries),
                all_unreferenced=plan.all_unreferenced,
            )
            service = SkillContentReclamationService(self._session, self._policy)
            content = await service.preview(
                user_id,
                actual.content.requested_trees,
                all_unreferenced=plan.all_unreferenced,
            )
            if not same_reclamation_actions(content, actual.content):
                raise SkillContentError("HEAD_CHANGED", "content reclamation forecast has changed")
            released = await service.apply(user_id, content)
            return CompactionReclamationResult(compacted, retired, released)
