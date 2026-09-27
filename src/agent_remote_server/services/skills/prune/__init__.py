"""
提供账户/单项 prune 的完整内部候选与精确执行，公开持久化确认另行接入。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_prune_claims import SkillPruneClaimRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.compaction.build import CompactionBuilder
from agent_remote_server.services.skills.compaction.reclamation import same_reclamation_actions
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.services.skills.gc.build import affected_objects
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.prune.build import build_prune
from agent_remote_server.services.skills.prune.claims import source_key
from agent_remote_server.services.skills.prune.plan import PrunePlan, PruneResult, PruneScope
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillPruneService:
    """
    预览已固定全部可执行组，应用阶段只完整重验并执行，不能再跳过阻断。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享外层事务、真实内容卷及保留策略，不建立独立提交边界。

        :param session (AsyncSession): 调用方最终提交的事务
        :param store (PrivateObjectStore): 私有不可变对象卷
        :param policy (SkillStoragePolicy): 当前额度与保留策略
        """
        self._session = session
        self._store = store
        self._policy = policy
        self._queries = SkillStateQueryService(session, store, policy)
        self._repository = SkillRetentionRepository(session)
        self._compactor = CompactionBuilder(MigrationContent(self._queries, store), policy)

    async def preview(
        self,
        user_id: UUID,
        selector: SkillStateSelector,
        *,
        all_unreferenced: bool = False,
        cutoff: datetime | None = None,
    ) -> PrunePlan:
        """
        先授权明确账户和稳定来源，再固定时间分析全部候选；提交预览不写任何记录。

        :param user_id (UUID): 当前认证所有者
        :param selector (SkillStateSelector): 明确账户与单项或目录范围
        :param all_unreferenced (bool): 是否显式提前结束历史等待
        :param cutoff (datetime | None): 重验时沿用原分析截止，默认当前时间
        :return PrunePlan: 完整规范范围、阻断、整理、损失和内容预测
        """
        now = datetime.now(UTC)
        if cutoff is not None:
            if cutoff.utcoffset() is None or cutoff > now:
                raise SkillContentError("INVALID_REQUEST", "invalid prune cutoff")
            now = cutoff
        source = await self._queries.scope(
            user_id, selector.account_id, selector.scope, selector.skill
        )
        index = await self._repository.load(user_id)
        return await build_prune(
            index,
            PruneScope(user_id, selector.account_id, source),
            now,
            await self._queries.library.generation(user_id),
            self._compactor,
            self._repository,
            SkillContentGCRepository(self._session),
            self._policy,
            all_unreferenced=all_unreferenced,
        )

    async def apply(self, user_id: UUID, expected: PrunePlan) -> PruneResult:
        """
        原时间和稳定来源重建整份计划；整理、所选完整组和精确结算一起提交或撤销。

        :param user_id (UUID): 当前认证所有者
        :param expected (PrunePlan): 已审阅内部原计划，不能代替公开持久化回执
        :return PruneResult: 尚待调用方最终提交的完整实际结果
        """
        scope = expected.scope
        if scope.user_id != user_id:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "prune account not found")
        selector = SkillStateSelector(
            account_id=scope.account_id,
            scope="item" if scope.source_id is not None else "account-directory",
            skill=str(scope.source_id) if scope.source_id is not None else None,
        )
        async with retention_mutation(self._session, user_id):
            actual = await self.preview(
                user_id,
                selector,
                all_unreferenced=expected.all_unreferenced,
                cutoff=expected.cutoff,
            )
            if actual != expected:
                raise SkillContentError("HEAD_CHANGED", "prune candidate plan has changed")
            if not actual.content.ready:
                raise SkillContentError("CONTENT_REFERENCED", "prune content has blockers")
            compacted = None
            if actual.compaction is not None:
                compacted = await SkillDirectoryCompactionService(
                    self._session, self._store, self._policy
                ).apply(user_id, actual.compaction)
            retired: tuple[RetentionKey, ...] = ()
            if actual.retirement is not None:
                retired = await SkillHistoryRetirementService(self._session, self._policy).retire(
                    user_id,
                    scope.account_id,
                    tuple(row.key for row in actual.retirement.entries),
                    all_unreferenced=actual.all_unreferenced,
                )
            tree_keys = tuple(row.key for row in actual.trees)
            index = await self._repository.load(user_id)
            await SkillPruneClaimRepository(self._session).register(
                user_id,
                scope.account_id,
                source_key(scope),
                set(tree_keys) | affected_objects(index, tree_keys),
            )
            service = SkillContentReclamationService(self._session, self._policy)
            content = await service.preview(
                user_id,
                actual.content.requested_trees,
                objects=actual.content.requested_objects,
                all_unreferenced=actual.all_unreferenced,
            )
            if not same_reclamation_actions(content, actual.content):
                raise SkillContentError("HEAD_CHANGED", "prune content forecast has changed")
            released = await service.apply(user_id, content)
            return PruneResult(compacted, retired, released)
